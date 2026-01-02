from odoo import models, fields


class PosConfig(models.Model):
  _inherit = 'pos.config'

  shopify_location_id = fields.Char(
      'Shopify Location ID',
      help='External Shopify Location ID associated with this POS configuration',
      index=True,
      copy=False,
  )

  def _notify_shopify_orders(self, channel, data):
    """Notify all open POS sessions about new Shopify orders"""
    pos_sessions = self.env["pos.session"].search([("state", "!=", "closed")])
    for session in pos_sessions:
      session.config_id._notify(channel, data)
