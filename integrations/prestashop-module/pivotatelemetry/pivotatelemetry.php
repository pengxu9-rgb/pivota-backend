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

    /**
     * After this many failed deliveries a row stops being retried. It is NOT
     * deleted: it is marked `dead` and counted on the configuration page, so a
     * shop that was misconfigured for a week can say how much it lost.
     */
    const MAX_ATTEMPTS = 20;

    /** Outbox row states. `dead` rows are never selected by the drain again. */
    const STATUS_PENDING = 'pending';
    const STATUS_DEAD = 'dead';

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
        // Reset, in the back office, is uninstall + install. So uninstalling
        // keeps the endpoint and the signing secret (a merchant who reset the
        // module would otherwise have to rotate the secret and re-paste it),
        // and keeps the outbox table whenever anything is still queued in it.
        $this->confirmUponUninstall = $this->l(
            'Uninstalling keeps your endpoint, signing secret and any events still waiting to be sent.'
        );
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
        // The endpoint and the secret are only initialised when they are not
        // already set: a back-office **Reset** is uninstall + install, and
        // blanking them there would silently stop a working shop.
        if (Configuration::get('PIVOTA_TELEMETRY_ENDPOINT') === false) {
            Configuration::updateValue('PIVOTA_TELEMETRY_ENDPOINT', '');
        }
        if (Configuration::get('PIVOTA_TELEMETRY_SECRET') === false) {
            Configuration::updateValue('PIVOTA_TELEMETRY_SECRET', '');
        }

        return $this->registerHook('actionValidateOrder')
            && $this->registerHook('actionOrderStatusPostUpdate')
            && $this->registerHook('actionOrderSlipAdd');
    }

    /**
     * Uninstall keeps anything a merchant cannot re-create by themselves.
     *
     * The back office's **Reset** button is uninstall + install, so this runs
     * on a path a merchant takes to FIX the module. Dropping the outbox there
     * threw away every event the shop had not managed to deliver yet, and
     * deleting PIVOTA_TELEMETRY_SECRET forced a rotation in the Pivota console
     * plus a re-paste — for a reset. So: the table is dropped only when it is
     * empty of work, and the endpoint and the secret are never deleted. Only
     * the cron token, which install() mints again, goes.
     */
    public function uninstall()
    {
        $pending = $this->pendingCount();
        if ($pending > 0) {
            PrestaShopLogger::addLog(
                'Pivota telemetry uninstalled with ' . (int) $pending
                . ' event(s) still queued: the outbox table was KEPT so they can still be sent'
                . ' after a reinstall.',
                2,
                null,
                'PivotaTelemetry'
            );
        } else {
            Db::getInstance()->execute(
                'DROP TABLE IF EXISTS `' . _DB_PREFIX_ . self::OUTBOX_TABLE . '`'
            );
        }
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
            `status` VARCHAR(16) NOT NULL DEFAULT "' . pSQL(self::STATUS_PENDING) . '",
            `available_at` DATETIME NOT NULL,
            `date_add` DATETIME NOT NULL,
            PRIMARY KEY (`id_outbox`),
            KEY `pivota_outbox_due` (`status`, `available_at`, `id_outbox`)
        ) ENGINE=' . _MYSQL_ENGINE_ . ' DEFAULT CHARSET=utf8';

        return Db::getInstance()->execute($sql);
    }

    /**
     * Rows the drain gave up on. Surfaced on the configuration page: a merchant
     * whose endpoint was wrong for a week must be able to SEE that events were
     * lost, rather than find a silently shorter ledger in Pivota.
     */
    public function deadCount()
    {
        return $this->countByStatus(self::STATUS_DEAD);
    }

    private function pendingCount()
    {
        return $this->countByStatus(self::STATUS_PENDING);
    }

    private function countByStatus($status)
    {
        $table = _DB_PREFIX_ . self::OUTBOX_TABLE;
        // The table is gone after an uninstall, and getValue() on a missing
        // table raises in debug mode, so check first.
        $exists = Db::getInstance()->executeS('SHOW TABLES LIKE "' . pSQL($table) . '"');
        if (!$exists) {
            return 0;
        }

        return (int) Db::getInstance()->getValue(
            'SELECT COUNT(*) FROM `' . $table . '`
             WHERE `status` = "' . pSQL($status) . '"'
        );
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
            $submittedEndpoint = trim((string) Tools::getValue('PIVOTA_TELEMETRY_ENDPOINT'));
            // https ONLY. The body is signed, not encrypted: over http the
            // whole payload — and a valid signature for it — travels in
            // cleartext for anyone on the path to read and replay within the
            // receiver's 300 s window. A typo'd scheme must be a form error,
            // not a silently downgraded shop.
            if ($submittedEndpoint !== '' && stripos($submittedEndpoint, 'https://') !== 0) {
                $output .= $this->displayError(
                    $this->l('The endpoint must start with https:// — telemetry is never sent over http.')
                );
            } else {
                Configuration::updateValue('PIVOTA_TELEMETRY_ENDPOINT', $submittedEndpoint);
                $submittedSecret = trim((string) Tools::getValue('PIVOTA_TELEMETRY_SECRET'));
                if ($submittedSecret !== '') {
                    Configuration::updateValue('PIVOTA_TELEMETRY_SECRET', $submittedSecret);
                }
                $output .= $this->displayConfirmation($this->l('Settings saved.'));
            }
        }
        $output .= $this->displayInformation(
            $this->l('Cron URL (run every minute or two):') . ' ' . $this->cronUrl()
        );
        $dead = $this->deadCount();
        if ($dead > 0) {
            $output .= $this->displayError(
                sprintf(
                    $this->l('%d event(s) could not be delivered and are no longer being retried. Contact Pivota support to have them replayed.'),
                    (int) $dead
                )
            );
        }

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
                        'desc' => $this->l('The https URL Pivota gave you, ending in your store id. http is refused.'),
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
                'status' => pSQL(self::STATUS_PENDING),
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
