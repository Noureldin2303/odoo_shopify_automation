/** @odoo-module */

import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/store/pos_store";

patch(PosStore.prototype, {
  async setup() {
    await super.setup(...arguments);
    this.data.connectWebSocket("SHOPIFY_ORDER_CREATE", this.onShopifyOrderCreate.bind(this));
  },

  async onShopifyOrderCreate(data) {
    this.notification.add(
      `New Shopify Order: ${data.order_name} - Customer: ${data.partner_name}`,
      {
        type: "info",
        title: "Shopify Order",
      }
    );
  },
});
