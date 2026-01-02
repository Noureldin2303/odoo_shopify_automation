/** @odoo-module */

import { patch } from "@web/core/utils/patch";
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { SelectCreateDialog } from "@web/views/view_dialogs/select_create_dialog";

patch(ControlButtons.prototype, {
  onClickQuotation() {
    // Start with the default domain
    const domain = [
      ["state", "!=", "cancel"],
      ["invoice_status", "!=", "invoiced"],
      ["currency_id", "=", this.pos.currency.id],
    ];

    // Add Shopify-specific filter: only show Shopify orders for current POS config
    // Show all non-Shopify orders OR Shopify orders matching current config
    if (this.pos.config && this.pos.config.id) {
      domain.push(
        "|",
        "|",
        ["shopify_order_source", "=", false],
        ["shopify_order_source", "!=", "shopify"],
        "&",
        ["shopify_order_source", "=", "shopify"],
        ["config_id", "=", this.pos.config.id]
      );
    }

    this.dialog.add(SelectCreateDialog, {
      resModel: "sale.order",
      noCreate: true,
      multiSelect: false,
      domain: domain,
      onSelected: async (resIds) => {
        await this.pos.onClickSaleOrder(resIds[0]);
      },
    });
  },
});
