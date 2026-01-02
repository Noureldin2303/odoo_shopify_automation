from odoo import models, fields, api
import requests
from odoo.exceptions import UserError
from odoo.tools.translate import _


class ShopifyCustomer(models.Model):
  _name = 'shopify.customer'
  _description = 'Shopify Customer Mapping'
  _rec_name = 'shopify_customer_id'

  shopify_customer_id = fields.Char('Shopify Customer ID', required=True)
  odoo_partner_id = fields.Many2one('res.partner', string='Odoo Partner', required=True)
  instance_id = fields.Many2one('shopify.instance', string='Shopify Instance', required=True)
  sync_status = fields.Selection([
      ('synced', 'Synced'),
      ('pending', 'Pending'),
      ('error', 'Error'),
  ],
                                 string='Sync Status',
                                 default='pending')
  last_sync = fields.Datetime('Last Sync')
  active = fields.Boolean('Active', default=True)
  note = fields.Text('Notes')

  _sql_constraints = [
      ('uniq_shopify_customer_instance', 'unique(shopify_customer_id, instance_id)',
       'This Shopify customer is already mapped for this instance!'),
  ]

  def import_customers_from_shopify(self, instance):
    """
        Import customers from Shopify for the given instance.
        Creates queue jobs and logs results.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))
    url = f"{instance.shop_url}/admin/api/2024-01/customers.json"
    try:
      response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)
      if response.status_code == 200:
        customers = response.json().get('customers', [])

        # Process each customer
        created_count = 0
        updated_count = 0
        error_count = 0

        for shopify_customer in customers:
          try:
            # Check if customer already exists
            existing_mapping = self.search([('shopify_customer_id', '=',
                                             str(shopify_customer['id'])),
                                            ('instance_id', '=', instance.id)])

            if existing_mapping:
              # Update existing customer
              odoo_partner = existing_mapping.odoo_partner_id
              updated_count += 1
            else:
              # Create new Odoo partner
              customer_email = shopify_customer.get('email', '')
              customer_name = f"{shopify_customer.get('first_name', '')} {shopify_customer.get('last_name', '')}".strip(
              )

              # Check if partner already exists by email
              existing_partner = self.env['res.partner'].search([('email', '=', customer_email)],
                                                                limit=1)
              if existing_partner:
                odoo_partner = existing_partner
              else:
                odoo_partner = self.env['res.partner'].create({
                    'name': customer_name or 'Unknown Customer',
                    'email': customer_email,
                    'phone': shopify_customer.get('phone', ''),
                    'is_company': False,
                    'customer_rank': 1,
                })

              created_count += 1

            # Create or update mapping
            mapping_vals = {
                'shopify_customer_id': str(shopify_customer['id']),
                'odoo_partner_id': odoo_partner.id,
                'instance_id': instance.id,
                'sync_status': 'synced',
                'last_sync': fields.Datetime.now(),
            }

            if existing_mapping:
              existing_mapping.write(mapping_vals)
            else:
              self.create(mapping_vals)

          except Exception as e:
            error_count += 1

        return customers
      else:
        raise UserError(_(f'Failed to import customers: {response.text}'))
    except Exception as e:
      raise UserError(_(f'Exception during customer import: {str(e)}'))

  @api.model
  def _run_customer_import_cron(self):
    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])
    for instance in instances:
      self.import_customers_from_shopify(instance)
