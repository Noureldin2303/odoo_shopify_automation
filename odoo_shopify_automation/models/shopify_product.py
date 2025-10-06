from odoo import models, fields, api
import requests
import logging
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)


class ShopifyProduct(models.Model):
  _name = 'shopify.product'
  _description = 'Shopify Product Mapping'
  _rec_name = 'name'

  name = fields.Char('Shopify Product Name')
  shopify_product_id = fields.Char('Shopify Product ID', required=True)
  shopify_variant_id = fields.Char('Shopify Variant ID')
  odoo_product_id = fields.Many2one('product.product', string='Odoo Product', required=True)
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
      ('uniq_shopify_variant_instance', 'unique(shopify_variant_id, instance_id)',
       'This Shopify variant is already mapped for this instance!'),
  ]

  def import_products_from_shopify(self, instance):
    """
        Import products from Shopify for the given instance.
        Creates queue jobs and logs results.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    url = f"{instance.shop_url}/admin/api/2024-07/products.json"

    try:
      # Use access_token if available, otherwise fall back to api_key/password
      if hasattr(instance, 'access_token') and instance.access_token:
        headers = {
            'X-Shopify-Access-Token': instance.access_token,
            'Content-Type': 'application/json',
        }
        response = requests.get(url, headers=headers, timeout=20)
      else:
        response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

      if response.status_code == 200:
        response_data = response.json()
        products = response_data.get('products', [])

        job = self.env['shopify.queue.job'].create({
            'name': f'Import Products ({instance.name})',
            'job_type': 'import_product',
            'instance_id': instance.id,
            'status': 'in_progress',
        })
        self.env['shopify.log'].create({
            'name':
                'Product Import Started',
            'log_type':
                'info',
            'job_id':
                job.id,
            'message':
                f'Starting import of {len(products)} products from Shopify instance {instance.name}',
        })

        # Process each product
        created_count = 0
        updated_count = 0
        error_count = 0

        for shopify_product in products:
          try:
            # Check if product already exists
            existing_mapping = self.search([('shopify_product_id', '=', str(shopify_product['id'])),
                                            ('instance_id', '=', instance.id)])

            if existing_mapping:
              # Update existing product
              odoo_product = existing_mapping.odoo_product_id

              # Update product information including image
              update_vals = {
                  'name': shopify_product.get('title', 'Unknown Product'),
                  'list_price': float(shopify_product.get('variants', [{}])[0].get('price', 0)),
                  'description': shopify_product.get('body_html', ''),
              }

              # Handle product image update
              if shopify_product.get('image') and shopify_product['image'].get('src'):
                try:
                  image_data = self._download_product_image(shopify_product['image']['src'])
                  if image_data:
                    update_vals['image_1920'] = image_data
                except Exception as img_error:
                  _logger.warning(
                      f"Failed to update image for product {shopify_product.get('title')}: {str(img_error)}"
                  )

              odoo_product.write(update_vals)
              updated_count += 1
            else:
              # Create new Odoo product with image
              product_vals = {
                  'name': shopify_product.get('title', 'Unknown Product'),
                  'default_code': shopify_product.get('sku', ''),
                  'list_price': float(shopify_product.get('variants', [{}])[0].get('price', 0)),
                  'type': 'product',
                  'categ_id': self.env.ref('product.product_category_all').id,
                  'description': shopify_product.get('body_html', ''),
              }

              # Handle product image
              if shopify_product.get('image') and shopify_product['image'].get('src'):
                try:
                  image_data = self._download_product_image(shopify_product['image']['src'])
                  if image_data:
                    product_vals['image_1920'] = image_data
                    _logger.info(f"Downloaded image for product: {shopify_product.get('title')}")
                except Exception as img_error:
                  _logger.warning(
                      f"Failed to download image for product {shopify_product.get('title')}: {str(img_error)}"
                  )

              odoo_product = self.env['product.product'].create(product_vals)
              created_count += 1

            # Create or update mapping
            mapping_vals = {
                'name': shopify_product.get('title', 'Unknown Product'),
                'shopify_product_id': str(shopify_product['id']),
                'odoo_product_id': odoo_product.id,
                'instance_id': instance.id,
                'sync_status': 'synced',
                'last_sync': fields.Datetime.now(),
            }

            if existing_mapping:
              existing_mapping.write(mapping_vals)
            else:
              self.create(mapping_vals)

            # Handle variants if any
            variants = shopify_product.get('variants', [])
            for variant in variants:
              variant_mapping = self.search([('shopify_variant_id', '=', str(variant['id'])),
                                             ('instance_id', '=', instance.id)])

              if not variant_mapping:
                # Create variant mapping (using same Odoo product for now)
                self.create({
                    'name':
                        f"{shopify_product.get('title', 'Unknown Product')} - {variant.get('title', 'Default')}",
                    'shopify_product_id':
                        str(shopify_product['id']),
                    'shopify_variant_id':
                        str(variant['id']),
                    'odoo_product_id':
                        odoo_product.id,
                    'instance_id':
                        instance.id,
                    'sync_status':
                        'synced',
                    'last_sync':
                        fields.Datetime.now(),
                })

          except Exception as e:
            error_count += 1
            self.env['shopify.log'].create({
                'name':
                    'Product Import Error',
                'log_type':
                    'error',
                'job_id':
                    job.id,
                'message':
                    f'Error importing product {shopify_product.get("title", "Unknown")}: {str(e)}',
            })

        # Update job status
        job.write({'status': 'done'})
        self.env['shopify.log'].create({
            'name':
                'Product Import Completed',
            'log_type':
                'info',
            'job_id':
                job.id,
            'message':
                f'Import completed: {created_count} created, {updated_count} updated, {error_count} errors',
        })

        return products
      else:
        _logger.error(f"Product import failed with status {response.status_code}: {response.text}")
        job = self.env['shopify.queue.job'].create({
            'name': f'Import Products ({instance.name})',
            'job_type': 'import_product',
            'instance_id': instance.id,
            'status': 'failed',
            'error_message': f'HTTP {response.status_code}: {response.text}',
        })
        self.env['shopify.log'].create({
            'name': 'Product Import Error',
            'log_type': 'error',
            'job_id': job.id,
            'message': f'Failed to import products - HTTP {response.status_code}: {response.text}',
        })
        raise UserError(_(f'Failed to import products - HTTP {response.status_code}: {response.text}'))
    except Exception as e:
      _logger.exception(f"Exception during product import from {instance.name}: {str(e)}")
      job = self.env['shopify.queue.job'].create({
          'name': f'Import Products ({instance.name})',
          'job_type': 'import_product',
          'instance_id': instance.id,
          'status': 'failed',
          'error_message': str(e),
      })
      self.env['shopify.log'].create({
          'name': 'Product Import Exception',
          'log_type': 'error',
          'job_id': job.id,
          'message': str(e),
      })
      raise UserError(_(f'Exception during product import: {str(e)}'))

  def export_products_to_shopify(self, instance, products=None):
    """
        Export products to Shopify for the given instance.
        Creates queue jobs and logs results.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    # If no products specified, get all non-synced products for this instance
    if products is None:
      products = self.search([('instance_id', '=', instance.id), ('sync_status', '!=', 'synced')])

    job = self.env['shopify.queue.job'].create({
        'name': f'Export Products ({instance.name})',
        'job_type': 'export_product',
        'instance_id': instance.id,
        'status': 'in_progress',
    })

    self.env['shopify.log'].create({
        'name':
            'Product Export Started',
        'log_type':
            'info',
        'job_id':
            job.id,
        'message':
            f'Starting export of {len(products)} products to Shopify instance {instance.name}',
    })

    exported_count = 0
    error_count = 0

    for product_mapping in products:
      try:
        odoo_product = product_mapping.odoo_product_id

        # Prepare product data for Shopify
        product_data = {
            'product': {
                'title':
                    odoo_product.name,
                'body_html':
                    odoo_product.description or '',
                'vendor':
                    odoo_product.company_id.name if odoo_product.company_id else 'Odoo',
                'product_type':
                    odoo_product.categ_id.name if odoo_product.categ_id else 'Default',
                'tags':
                    '',  # Can be customized based on specific requirements
                'status':
                    'active' if odoo_product.active else 'draft',
                'variants': [{
                    'price':
                        str(odoo_product.list_price),
                    'sku':
                        odoo_product.default_code or '',
                    'inventory_quantity':
                        int(odoo_product.qty_available) if odoo_product.type == 'consu' else 0,
                    'inventory_management':
                        'shopify' if odoo_product.type == 'consu' else None,
                    'weight':
                        float(odoo_product.weight) if odoo_product.weight else 0,
                    'weight_unit':
                        'kg',
                    'barcode':
                        odoo_product.barcode or '',
                }]
            }
        }

        # Add product image if available
        if odoo_product.image_1920:
          import base64
          try:
            image_b64 = base64.b64decode(odoo_product.image_1920)
            # Upload image after product creation
            product_data['product']['images'] = [{
                'attachment': base64.b64encode(image_b64).decode('utf-8')
            }]
          except Exception as img_error:
            _logger.warning(
                f"Failed to prepare image for product {odoo_product.name}: {str(img_error)}")

        # Use access_token if available, otherwise fall back to api_key/password
        headers = {'Content-Type': 'application/json'}
        if hasattr(instance, 'access_token') and instance.access_token:
          headers['X-Shopify-Access-Token'] = instance.access_token
          auth = None
        else:
          auth = (instance.api_key, instance.password)

        # Check if product already exists in Shopify
        if product_mapping.shopify_product_id:
          # Update existing product
          url = f"{instance.shop_url}/admin/api/2024-07/products/{product_mapping.shopify_product_id}.json"
          if auth:
            response = requests.put(url, auth=auth, json=product_data, timeout=20)
          else:
            response = requests.put(url, headers=headers, json=product_data, timeout=20)
        else:
          # Create new product
          url = f"{instance.shop_url}/admin/api/2024-07/products.json"
          if auth:
            response = requests.post(url, auth=auth, json=product_data, timeout=20)
          else:
            response = requests.post(url, headers=headers, json=product_data, timeout=20)

        if response.status_code in [200, 201]:
          response_data = response.json()
          shopify_product = response_data.get('product', {})

          # Update mapping with Shopify product ID and variant ID
          update_vals = {
              'shopify_product_id': str(shopify_product.get('id')),
              'sync_status': 'synced',
              'last_sync': fields.Datetime.now(),
          }

          # Update variant ID if available
          variants = shopify_product.get('variants', [])
          if variants:
            update_vals['shopify_variant_id'] = str(variants[0].get('id'))

          product_mapping.write(update_vals)
          exported_count += 1

          self.env['shopify.log'].create({
              'name': 'Product Export Success',
              'log_type': 'info',
              'job_id': job.id,
              'message': f'Successfully exported product {odoo_product.name} to Shopify',
          })
        else:
          error_count += 1
          self.env['shopify.log'].create({
              'name':
                  'Product Export Error',
              'log_type':
                  'error',
              'job_id':
                  job.id,
              'message':
                  f'Failed to export product {odoo_product.name} - HTTP {response.status_code}: {response.text}',
          })

      except Exception as e:
        error_count += 1
        self.env['shopify.log'].create({
            'name':
                'Product Export Exception',
            'log_type':
                'error',
            'job_id':
                job.id,
            'message':
                f'Exception exporting product {product_mapping.odoo_product_id.name}: {str(e)}',
        })

    # Update job status
    job.write({'status': 'done'})
    self.env['shopify.log'].create({
        'name': 'Product Export Completed',
        'log_type': 'info',
        'job_id': job.id,
        'message': f'Export completed: {exported_count} exported, {error_count} errors',
    })

    return True

  def export_single_product_to_shopify(self, instance, odoo_product):
    """
        Export a single Odoo product to Shopify.
        Creates a mapping if it doesn't exist.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    if not odoo_product:
      raise UserError(_('No product provided.'))

    # Check if product mapping already exists
    existing_mapping = self.search([('odoo_product_id', '=', odoo_product.id),
                                    ('instance_id', '=', instance.id)],
                                   limit=1)

    if not existing_mapping:
      # Create new mapping
      existing_mapping = self.create({
          'name': odoo_product.name,
          'shopify_product_id': '',  # Will be filled after successful export
          'odoo_product_id': odoo_product.id,
          'instance_id': instance.id,
          'sync_status': 'pending',
      })

    # Export the product
    return self.export_products_to_shopify(instance, existing_mapping)

  @api.model
  def _run_product_import_cron(self):
    """
        Cron job method to automatically import products from all active Shopify instances.
        """
    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])
    for instance in instances:
      try:
        self.import_products_from_shopify(instance)
      except Exception as e:
        self.env['shopify.log'].create({
            'name': 'Cron Product Import Error',
            'log_type': 'error',
            'message': f'Error importing products for instance {instance.name}: {str(e)}',
        })

  @api.model
  def _run_product_export_cron(self):
    """
        Cron job method to automatically export pending products to all active Shopify instances.
        """
    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])
    for instance in instances:
      try:
        # Find products that need to be exported (pending or error status)
        pending_products = self.search([('instance_id', '=', instance.id),
                                        ('sync_status', 'in', ['pending', 'error'])])
        if pending_products:
          self.export_products_to_shopify(instance, pending_products)
      except Exception as e:
        self.env['shopify.log'].create({
            'name': 'Cron Product Export Error',
            'log_type': 'error',
            'message': f'Error exporting products for instance {instance.name}: {str(e)}',
        })

  def _download_product_image(self, image_url):
    import base64

    try:
      _logger.info(f"Downloading image from: {image_url}")
      response = requests.get(image_url, timeout=30)

      if response.status_code == 200:
        image_data = base64.b64encode(response.content)
        _logger.info(f"Successfully downloaded image, size: {len(response.content)} bytes")
        return image_data
      else:
        _logger.error(f"Failed to download image, status: {response.status_code}")
        return None

    except Exception as e:
      _logger.error(f"Error downloading image from {image_url}: {str(e)}")
      return None
