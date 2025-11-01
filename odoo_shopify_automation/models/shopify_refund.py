from odoo import models, fields


class ShopifyRefund(models.Model):
  _name = 'shopify.refund'
  _description = 'Shopify Refund Mapping'
  _rec_name = 'name'

  name = fields.Char('Refund Reference')
  shopify_refund_id = fields.Char('Shopify Refund ID', required=True)
  order_mapping_id = fields.Many2one('shopify.order',
                                     string='Shopify Order Mapping',
                                     required=True,
                                     ondelete='cascade')
  move_id = fields.Many2one('account.move', string='Credit Note', ondelete='set null')
  instance_id = fields.Many2one('shopify.instance', string='Shopify Instance', required=True)
  amount = fields.Monetary('Amount', currency_field='currency_id')
  currency_id = fields.Many2one('res.currency',
                                string='Currency',
                                required=True,
                                default=lambda self: self.env.company.currency_id)
  refund_date = fields.Datetime('Refund Date')
  state = fields.Selection([
      ('draft', 'Draft'),
      ('posted', 'Posted'),
      ('cancel', 'Cancelled'),
  ],
                           string='Status',
                           default='draft')
  note = fields.Char('Notes')

  _sql_constraints = [('shopify_refund_unique', 'unique(shopify_refund_id, instance_id)',
                       'This Shopify refund has already been imported.')]
