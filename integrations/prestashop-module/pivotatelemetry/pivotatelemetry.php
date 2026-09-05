<?php
/**
 * Pivota commerce telemetry for PrestaShop 1.7 / 8.
 *
 * PrestaShop sends no webhooks. Its extension point is a hook that runs INSIDE
 * the shopper's own request, so this module never talks to the network from a
 * hook: each hook writes ONE row into a local outbox table, and
 * controllers/front/drain.php — driven by cron — is the only thing that opens
 * a socket. Same rule, and the same reason, as the Salesforce B2C cartridge in
 * integrations/sfcc-cartridge/: a Pivota outage must never slow down or break
 * a checkout.
 *
 * The payload contract is fixed on both sides and pinned by
 * tests/test_prestashop_module_contract.py against
 * services/prestashop_event_adapter.py.
 *
 * NOTHING personal is serialized: no name, no e-mail, no postal details, no
 * payment instrument. Only order/cart/customer IDs, money totals, currency,
 * the resolved state key, and the payment module's technical name.
 *
 * This file has never been run through a PHP linter — there is no php binary
 * on the machine it was written on. Review it as unlinted source.
 */

if (!defined('_PS_VERSION_')) {
    exit;
}

class PivotaTelemetry extends Module
{
    const OUTBOX_TABLE = 'pivota_telemetry_outbox';

    /** Bounds the drain, mirrored by the receiver's own 1..100 batch rule. */
    const MAX_EVENTS_PER_BATCH = 100;
    const MAX_BATCHES_PER_RUN = 10;

    /** After this many failed deliveries a row is dropped, not retried forever. */
    const MAX_ATTEMPTS = 20;

    public function __construct()
    {
        $this->name = 'pivotatelemetry';
        $this->tab = 'analytics_stats';
        $this->version = '1.0.0';
        $this->author = 'Pivota';
        $this->need_instance = 0;
        $this->ps_versions_compliancy = array('min' => '1.7.0.0', 'max' => _PS_VERSION_);
        $this->bootstrap = true;

        parent::__construct();

        $this->displayName = $this->l('Pivota Commerce Telemetry');
        $this->description = $this->l(
            'Signs and forwards order and credit-slip events to Pivota. No personal data leaves the shop.'
        );
        $this->confirmUponUninstall = $this->l('Uninstalling drops any events still waiting to be sent.');
    }

    /**
     * Create the outbox, mint the cron token, register the three hooks.
     *
     * actionOrderStatusPostUpdate is registered rather than
     * actionOrderStatusUpdate: OrderHistory fires the Update variant BEFORE
     * the new state is written, so at that point the order still carries the
     * OLD current_state and the OLD total_paid_real.
     */
    public function install()
    {
        if (!parent::install()) {
            return false;
        }
        if (!$this->createOutboxTable()) {
            return false;
        }
        // The cron token guards the drain controller, which is a public URL.
        // It is NOT the signing secret and is deliberately shown in the back
        // office so the merchant can build the cron line.
        Configuration::updateValue('PIVOTA_TELEMETRY_CRON_TOKEN', Tools::passwdGen(48));
        Configuration::updateValue('PIVOTA_TELEMETRY_ENDPOINT', '');
        Configuration::updateValue('PIVOTA_TELEMETRY_STORE_ID', '');
        Configuration::updateValue('PIVOTA_TELEMETRY_SECRET', '');

        return $this->registerHook('actionValidateOrder')
            && $this->registerHook('actionOrderStatusPostUpdate')
            && $this->registerHook('actionOrderSlipAdd');
    }

    public function uninstall()
    {
        Db::getInstance()->execute(
            'DROP TABLE IF EXISTS `' . _DB_PREFIX_ . self::OUTBOX_TABLE . '`'
        );
        Configuration::deleteByName('PIVOTA_TELEMETRY_ENDPOINT');
        Configuration::deleteByName('PIVOTA_TELEMETRY_STORE_ID');
        Configuration::deleteByName('PIVOTA_TELEMETRY_SECRET');
        Configuration::deleteByName('PIVOTA_TELEMETRY_CRON_TOKEN');

        return parent::uninstall();
    }

    private function createOutboxTable()
    {
        $sql = 'CREATE TABLE IF NOT EXISTS `' . _DB_PREFIX_ . self::OUTBOX_TABLE . '` (
            `id_outbox` INT UNSIGNED NOT NULL AUTO_INCREMENT,
            `event_id` VARCHAR(191) NOT NULL,
            `payload` LONGTEXT NOT NULL,
            `attempts` INT UNSIGNED NOT NULL DEFAULT 0,
            `available_at` DATETIME NOT NULL,
            `date_add` DATETIME NOT NULL,
            PRIMARY KEY (`id_outbox`),
            KEY `pivota_outbox_due` (`available_at`, `id_outbox`)
        ) ENGINE=' . _MYSQL_ENGINE_ . ' DEFAULT CHARSET=utf8';

        return Db::getInstance()->execute($sql);
    }

    // ---- configuration ------------------------------------------------------

    /**
     * The configuration page. The secret is a password field and its stored
     * value is NEVER rendered back: submitting the form with the field left
     * empty keeps whatever is already stored.
     */
    public function getContent()
    {
        $output = '';
        if (Tools::isSubmit('submitPivotaTelemetry')) {
            Configuration::updateValue(
                'PIVOTA_TELEMETRY_ENDPOINT',
                trim((string) Tools::getValue('PIVOTA_TELEMETRY_ENDPOINT'))
            );
            Configuration::updateValue(
                'PIVOTA_TELEMETRY_STORE_ID',
                trim((string) Tools::getValue('PIVOTA_TELEMETRY_STORE_ID'))
            );
            $submittedSecret = trim((string) Tools::getValue('PIVOTA_TELEMETRY_SECRET'));
            if ($submittedSecret !== '') {
                Configuration::updateValue('PIVOTA_TELEMETRY_SECRET', $submittedSecret);
            }
            $output .= $this->displayConfirmation($this->l('Settings saved.'));
        }
        $output .= $this->displayInformation(
            $this->l('Cron URL (run every minute or two):') . ' ' . $this->cronUrl()
        );

        return $output . $this->renderForm();
    }

    public function cronUrl()
    {
        // The non-rewritten form is used deliberately: the rewritten
        // /module/<module>/<controller> URL carries a language segment, which
        // a cron line should not have to know.
        return $this->context->link->getModuleLink(
            $this->name,
            'drain',
            array('token' => Configuration::get('PIVOTA_TELEMETRY_CRON_TOKEN')),
            true
        );
    }

    private function renderForm()
    {
        $fields = array(
            'form' => array(
                'legend' => array('title' => $this->l('Pivota telemetry')),
                'input' => array(
                    array(
                        'type' => 'text',
                        'label' => $this->l('Endpoint'),
                        'name' => 'PIVOTA_TELEMETRY_ENDPOINT',
                        'desc' => $this->l('The https URL Pivota gave you, ending in your store id.'),
                        'required' => true,
                    ),
                    array(
                        'type' => 'text',
                        'label' => $this->l('Store id'),
                        'name' => 'PIVOTA_TELEMETRY_STORE_ID',
                        'required' => true,
                    ),
                    array(
                        // Never echoed: the stored value is not put back into
                        // this field, so the page cannot leak it.
                        'type' => 'password',
                        'label' => $this->l('Signing secret'),
                        'name' => 'PIVOTA_TELEMETRY_SECRET',
                        'desc' => $this->l('Shown once when provisioned. Leave empty to keep the current one.'),
                        'required' => false,
                    ),
                ),
                'submit' => array('title' => $this->l('Save'), 'name' => 'submitPivotaTelemetry'),
            ),
        );

        $helper = new HelperForm();
        $helper->module = $this;
        $helper->name_controller = $this->name;
        $helper->token = Tools::getAdminTokenLite('AdminModules');
        $helper->currentIndex = AdminController::$currentIndex . '&configure=' . $this->name;
        $helper->submit_action = 'submitPivotaTelemetry';
        $helper->fields_value = array(
            'PIVOTA_TELEMETRY_ENDPOINT' => Configuration::get('PIVOTA_TELEMETRY_ENDPOINT'),
            'PIVOTA_TELEMETRY_STORE_ID' => Configuration::get('PIVOTA_TELEMETRY_STORE_ID'),
            // Intentionally blank. See the comment on the field above.
            'PIVOTA_TELEMETRY_SECRET' => '',
        );

        return $helper->generateForm(array($fields));
    }

    // ---- hooks: enqueue only, never the network -----------------------------

    /**
     * @param array $params order, orderStatus, cart, customer, currency
     */
    public function hookActionValidateOrder($params)
    {
        try {
            if (empty($params['order']) || !Validate::isLoadedObject($params['order'])) {
                return;
            }
            $order = $params['order'];
            $state = isset($params['orderStatus']) ? $params['orderStatus'] : null;
            $this->enqueue('actionValidateOrder', $order, $state, null);
        } catch (Exception $exception) {
            $this->logFailure($exception);
        }
    }

    /**
     * @param array $params newOrderStatus, oldOrderStatus, id_order
     */
    public function hookActionOrderStatusPostUpdate($params)
    {
        try {
            if (empty($params['id_order']) || empty($params['newOrderStatus'])) {
                return;
            }
            $order = new Order((int) $params['id_order']);
            if (!Validate::isLoadedObject($order)) {
                return;
            }
            $this->enqueue('actionOrderStatusPostUpdate', $order, $params['newOrderStatus'], null);
        } catch (Exception $exception) {
            $this->logFailure($exception);
        }
    }

    /**
     * @param array $params order, productList, qtyList
     *
     * The hook does NOT carry the OrderSlip it just created
     * (src/Adapter/Order/Refund/OrderSlipCreator.php passes only those three),
     * so the newest slip for the order is read back here. productList/qtyList
     * are deliberately ignored: per-line detail is not telemetry Pivota needs.
     */
    public function hookActionOrderSlipAdd($params)
    {
        try {
            if (empty($params['order']) || !Validate::isLoadedObject($params['order'])) {
                return;
            }
            $order = $params['order'];
            $slip = Db::getInstance()->getRow(
                'SELECT `id_order_slip`, `amount`, `shipping_cost_amount`,
                        `total_products_tax_incl`, `total_shipping_tax_incl`, `date_add`
                 FROM `' . _DB_PREFIX_ . 'order_slip`
                 WHERE `id_order` = ' . (int) $order->id . '
                 ORDER BY `id_order_slip` DESC'
            );
            if (!$slip) {
                return;
            }
            $this->enqueue('actionOrderSlipAdd', $order, null, $slip);
        } catch (Exception $exception) {
            $this->logFailure($exception);
        }
    }

    // ---- payload ------------------------------------------------------------

    /**
     * Resolve a shop-specific order-state id into Pivota's fixed vocabulary.
     *
     * The receiver must never key money on `current_state`: order-state ids are
     * rows in this shop's own table. There is deliberately no separate
     * payment-error key in this map: PrestaShop has none, and asking
     * Configuration for a key it does not have returns false, which would then
     * compare equal to order state 0 and turn every unmatched state into a
     * payment failure. PS_OS_ERROR is the payment-failure state.
     */
    private function stateKey($idState)
    {
        $idState = (int) $idState;
        if (!$idState) {
            return 'other';
        }
        $map = array(
            'PS_OS_PAYMENT' => 'payment',
            'PS_OS_CANCELED' => 'canceled',
            'PS_OS_REFUND' => 'refund',
            'PS_OS_ERROR' => 'error',
            'PS_OS_SHIPPING' => 'shipped',
            'PS_OS_DELIVERED' => 'delivered',
        );
        foreach ($map as $configurationKey => $stateKey) {
            if ((int) Configuration::get($configurationKey) === $idState) {
                return $stateKey;
            }
        }

        return 'other';
    }

    private function stateFlags($state)
    {
        // `template` is NOT here: on OrderState it is a multilang string, not a
        // boolean. These four are the booleans Pivota reads or diagnoses with.
        return array(
            'paid' => $state ? (bool) $state->paid : false,
            'shipped' => $state ? (bool) $state->shipped : false,
            'delivery' => $state ? (bool) $state->delivery : false,
            'logable' => $state ? (bool) $state->logable : false,
        );
    }

    private function currencyIso($order)
    {
        $currency = new Currency((int) $order->id_currency);

        return Validate::isLoadedObject($currency) ? (string) $currency->iso_code : '';
    }

    private function enqueue($hook, $order, $state, $slip)
    {
        $idState = $state ? (int) $state->id : (int) $order->current_state;
        if (!$state && $idState) {
            $state = new OrderState($idState);
            if (!Validate::isLoadedObject($state)) {
                $state = null;
            }
        }
        $slipId = $slip ? (int) $slip['id_order_slip'] : 0;
        $eventId = $hook . ':' . (int) $order->id . ':' . ($slipId ? $slipId : $idState);

        $payload = array(
            'event_id' => $eventId,
            'hook' => $hook,
            'occurred_at' => gmdate('c'),
            'order' => array(
                'id' => (int) $order->id,
                'reference' => (string) $order->reference,
                'id_cart' => (int) $order->id_cart,
                'id_customer' => (int) $order->id_customer,
                'currency' => $this->currencyIso($order),
                'current_state' => $idState,
                'state_key' => $this->stateKey($idState),
                'state_flags' => $this->stateFlags($state),
                'valid' => (bool) $order->valid,
                'total_paid_tax_incl' => (string) $order->total_paid_tax_incl,
                'total_paid_real' => (string) $order->total_paid_real,
                'payment_module' => (string) $order->module,
                'date_add' => (string) $order->date_add,
                'date_upd' => (string) $order->date_upd,
            ),
            'order_slip' => $slip ? array(
                'id' => $slipId,
                'amount' => (string) $slip['amount'],
                'shipping_cost_amount' => (string) $slip['shipping_cost_amount'],
                'total_products_tax_incl' => (string) $slip['total_products_tax_incl'],
                'total_shipping_tax_incl' => (string) $slip['total_shipping_tax_incl'],
                'date_add' => (string) $slip['date_add'],
            ) : null,
        );

        $encoded = json_encode($payload);
        if ($encoded === false) {
            return;
        }
        $now = date('Y-m-d H:i:s');
        Db::getInstance()->insert(
            self::OUTBOX_TABLE,
            array(
                'event_id' => pSQL($eventId),
                'payload' => pSQL($encoded, true),
                'attempts' => 0,
                'available_at' => pSQL($now),
                'date_add' => pSQL($now),
            )
        );
    }

    private function logFailure(Exception $exception)
    {
        // The message only; a payload is never logged.
        PrestaShopLogger::addLog(
            'Pivota telemetry could not enqueue an event: ' . $exception->getMessage(),
            2,
            null,
            'PivotaTelemetry'
        );
    }
}
