<?php
/**
 * The ONLY part of the Pivota module that touches the network.
 *
 * Cron drives it. Every hook in pivotatelemetry.php writes a row and returns,
 * so a Pivota outage can never slow a shopper's request; this controller then
 * drains that outbox in bounded batches and signs each POST with the shared
 * secret the merchant pasted into the module's configuration.
 *
 * Signature: sha256 HMAC over `timestamp + "." + body`, sent as
 * `X-Pivota-PrestaShop-Signature: sha256=<hex>` alongside the timestamp that
 * was signed. routes/prestashop_webhooks.py verifies exactly that string.
 *
 * Delivery rules:
 *   - at most MAX_EVENTS_PER_BATCH events per POST, MAX_BATCHES_PER_RUN POSTs
 *     per run, so one cron tick cannot run away;
 *   - 2xx deletes the rows;
 *   - anything else keeps them, increments `attempts`, backs off
 *     exponentially, and stops the run (the next tick retries);
 *   - a row that has failed MAX_ATTEMPTS times is dropped, so a permanently
 *     misconfigured shop does not grow an unbounded table.
 *
 * Unlinted: written on a machine with no php binary.
 */

if (!defined('_PS_VERSION_')) {
    exit;
}

class PivotaTelemetryDrainModuleFrontController extends ModuleFrontController
{
    public $auth = false;
    public $ssl = true;

    /** Give up on the socket rather than hold a cron worker open. */
    const TIMEOUT_SECONDS = 20;

    public function initContent()
    {
        parent::initContent();

        $expectedToken = (string) Configuration::get('PIVOTA_TELEMETRY_CRON_TOKEN');
        $suppliedToken = (string) Tools::getValue('token');
        if ($expectedToken === '' || !hash_equals($expectedToken, $suppliedToken)) {
            header('HTTP/1.1 403 Forbidden');
            $this->ajaxRenderJson(array('status' => 'forbidden'));

            return;
        }

        $endpoint = trim((string) Configuration::get('PIVOTA_TELEMETRY_ENDPOINT'));
        $secret = (string) Configuration::get('PIVOTA_TELEMETRY_SECRET');
        if ($endpoint === '' || $secret === '') {
            $this->ajaxRenderJson(array('status' => 'not_configured'));

            return;
        }

        $delivered = 0;
        $failed = 0;
        for ($batch = 0; $batch < PivotaTelemetry::MAX_BATCHES_PER_RUN; ++$batch) {
            $rows = $this->dueRows();
            if (!$rows) {
                break;
            }
            $result = $this->deliver($rows, $endpoint, $secret);
            if ($result) {
                $delivered += count($rows);
                $this->removeRows($rows);
                continue;
            }
            $failed += count($rows);
            $this->markRetry($rows);
            // Stop the run: the endpoint is down or the secret is wrong, and
            // the remaining rows would fail the same way.
            break;
        }

        $this->ajaxRenderJson(array(
            'status' => 'ok',
            'delivered' => $delivered,
            'failed' => $failed,
        ));
    }

    private function ajaxRenderJson(array $payload)
    {
        header('Content-Type: application/json');
        echo json_encode($payload);
        exit;
    }

    private function dueRows()
    {
        return Db::getInstance()->executeS(
            'SELECT `id_outbox`, `payload`, `attempts`
             FROM `' . _DB_PREFIX_ . PivotaTelemetry::OUTBOX_TABLE . '`
             WHERE `available_at` <= "' . pSQL(date('Y-m-d H:i:s')) . '"
             ORDER BY `id_outbox` ASC
             LIMIT ' . (int) PivotaTelemetry::MAX_EVENTS_PER_BATCH
        );
    }

    private function idList(array $rows)
    {
        $ids = array();
        foreach ($rows as $row) {
            $ids[] = (int) $row['id_outbox'];
        }

        return implode(',', $ids);
    }

    private function removeRows(array $rows)
    {
        if (!$rows) {
            return;
        }
        Db::getInstance()->execute(
            'DELETE FROM `' . _DB_PREFIX_ . PivotaTelemetry::OUTBOX_TABLE . '`
             WHERE `id_outbox` IN (' . $this->idList($rows) . ')'
        );
    }

    private function markRetry(array $rows)
    {
        foreach ($rows as $row) {
            $attempts = (int) $row['attempts'] + 1;
            if ($attempts >= PivotaTelemetry::MAX_ATTEMPTS) {
                Db::getInstance()->execute(
                    'DELETE FROM `' . _DB_PREFIX_ . PivotaTelemetry::OUTBOX_TABLE . '`
                     WHERE `id_outbox` = ' . (int) $row['id_outbox']
                );
                continue;
            }
            $delay = min(3600, pow(2, min($attempts, 10)) * 15);
            Db::getInstance()->execute(
                'UPDATE `' . _DB_PREFIX_ . PivotaTelemetry::OUTBOX_TABLE . '`
                 SET `attempts` = ' . $attempts . ',
                     `available_at` = "' . pSQL(date('Y-m-d H:i:s', time() + (int) $delay)) . '"
                 WHERE `id_outbox` = ' . (int) $row['id_outbox']
            );
        }
    }

    private function shopUrl()
    {
        return Tools::getShopDomainSsl(true, true);
    }

    /**
     * @return bool true only on a 2xx
     */
    private function deliver(array $rows, $endpoint, $secret)
    {
        $events = array();
        foreach ($rows as $row) {
            $decoded = json_decode($row['payload'], true);
            if (is_array($decoded)) {
                $events[] = $decoded;
            }
        }
        if (!$events) {
            // Nothing decodable: let the caller delete the rows rather than
            // retry a batch that can never be built.
            return true;
        }
        $body = json_encode(array(
            'events' => $events,
            'shop_url' => $this->shopUrl(),
        ));
        if ($body === false) {
            return true;
        }
        $timestamp = (string) time();
        $signature = hash_hmac('sha256', $timestamp . '.' . $body, $secret);
        $deliveryId = md5(uniqid('pivota', true));

        $headers = array(
            'Content-Type: application/json',
            'X-Pivota-PrestaShop-Signature: sha256=' . $signature,
            'X-Pivota-PrestaShop-Timestamp: ' . $timestamp,
            'X-Pivota-PrestaShop-Delivery-Id: ' . $deliveryId,
            'X-Pivota-PrestaShop-Shop-Url: ' . $this->shopUrl(),
        );

        $handle = curl_init($endpoint);
        if ($handle === false) {
            return false;
        }
        curl_setopt($handle, CURLOPT_POST, true);
        curl_setopt($handle, CURLOPT_POSTFIELDS, $body);
        curl_setopt($handle, CURLOPT_HTTPHEADER, $headers);
        curl_setopt($handle, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($handle, CURLOPT_TIMEOUT, self::TIMEOUT_SECONDS);
        curl_setopt($handle, CURLOPT_CONNECTTIMEOUT, self::TIMEOUT_SECONDS);
        curl_setopt($handle, CURLOPT_SSL_VERIFYPEER, true);
        curl_setopt($handle, CURLOPT_SSL_VERIFYHOST, 2);
        curl_exec($handle);
        $status = (int) curl_getinfo($handle, CURLINFO_HTTP_CODE);
        $error = curl_error($handle);
        curl_close($handle);

        if ($status >= 200 && $status < 300) {
            return true;
        }
        PrestaShopLogger::addLog(
            'Pivota telemetry delivery failed with HTTP ' . $status
            . ($error ? ' (' . $error . ')' : '')
            . ' [Pivota telemetry payload redacted]',
            2,
            null,
            'PivotaTelemetry'
        );

        return false;
    }
}
