from odoo import models, fields


class PosConfig(models.Model):
  _inherit = 'pos.config'

  shopify_location_id = fields.Char(
      'Shopify Location ID',
      help='External Shopify Location ID associated with this POS configuration',
      index=True,
      copy=False,
  )
