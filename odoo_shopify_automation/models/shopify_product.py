from odoo import models, fields, api
import requests
import logging
from odoo.exceptions import UserError, ValidationError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)


class ShopifyProduct(models.Model):
  _name = 'shopify.product'
  _description = 'Shopify Product Mapping'
  _rec_name = 'name'

  name = fields.Char('Shopify Product Name')
  shopify_product_id = fields.Char(
      'Shopify Product ID',
      required=True,
  )
  shopify_variant_id = fields.Char('Shopify Variant ID')
  odoo_product_id = fields.Many2one(
      'product.product',
      string='Odoo Product',
      required=True,
  )
  instance_id = fields.Many2one(
      'shopify.instance',
      string='Shopify Instance',
      required=True,
  )
  sync_status = fields.Selection(
      [
          ('synced', 'Synced'),
          ('pending', 'Pending'),
          ('error', 'Error'),
      ],
      string='Sync Status',
      default='pending',
  )
  last_sync = fields.Datetime('Last Sync')
  active = fields.Boolean('Active', default=True)
  note = fields.Text('Notes')

  # Enhanced fields for product details
  sku = fields.Char(
      'SKU Code',
      help='Stock Keeping Unit from Shopify',
  )
  stock_quantity = fields.Float(
      'Stock Quantity',
      help='Available stock from Shopify',
  )
  warehouse_location = fields.Selection(
      [
          ('online', 'Online Store'),
          ('warehouse', 'Physical Warehouse'),
          ('dropship', 'Dropshipping'),
          ('retail', 'Retail Store'),
      ],
      string='Product Location',
      default='online',
      help='Where the product is stored/sold from',
  )
  product_color = fields.Char(
      'Color',
      help='Product color variant',
  )
  shopify_inventory_item_id = fields.Char(
      'Shopify Inventory Item ID',
      help='Inventory item ID from Shopify',
  )
  shopify_location_id = fields.Char(
      'Shopify Location ID',
      help='Location ID from Shopify',
  )

  _sql_constraints = [
      ('uniq_shopify_variant_instance', 'unique(shopify_variant_id, instance_id)',
       'This Shopify variant is already mapped for this instance!'),
  ]

  def import_products_from_shopify(self, instance):
    """Import products from Shopify and keep variants in sync."""
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    url = f"{instance.shop_url}/admin/api/2024-07/products.json"

    try:
      if hasattr(instance, 'access_token') and instance.access_token:
        headers = {
            'X-Shopify-Access-Token': instance.access_token,
            'Content-Type': 'application/json',
        }
        response = requests.get(url, headers=headers, timeout=20)
      else:
        response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

      if response.status_code != 200:
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
        raise UserError(
            _(f'Failed to import products - HTTP {response.status_code}: {response.text}'))

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

      created_count = 0
      updated_count = 0
      error_count = 0

      ProductTemplate = self.env['product.template']

      for shopify_product in products:
        try:
          product_id_str = str(shopify_product.get('id'))
          variants = shopify_product.get('variants', []) or []
          if not variants:
            continue

          # Don't use base_price from first variant - we'll set each variant's price directly
          meaningful_options = self._get_meaningful_options(shopify_product)
          attribute_lines, attribute_info = self._prepare_attribute_data(meaningful_options)

          image_data = False
          image_src = (shopify_product.get('image') or {}).get('src')
          if image_src:
            try:
              image_data = self._download_product_image(image_src)
            except Exception as img_error:
              _logger.warning(
                  f"Failed to download image for product {shopify_product.get('title')}: {str(img_error)}"
              )

          existing_mappings = self.with_context(active_test=False).search([
              ('shopify_product_id', '=', product_id_str), ('instance_id', '=', instance.id)
          ])

          # First, check if a product template exists with this Shopify ID
          odoo_template = ProductTemplate.search([('shopify_external_id', '=', product_id_str)],
                                                 limit=1)

          # If not found by external ID, check existing mappings
          if not odoo_template and existing_mappings:
            odoo_template = existing_mappings[:1].odoo_product_id.product_tmpl_id

          if odoo_template:
            template_update_vals = {
                'name': shopify_product.get('title', 'Unknown Product'),
                'description': shopify_product.get('body_html', '') or '',
                'shopify_external_id': product_id_str,  # Update external ID
            }
            if image_data:
              template_update_vals['image_1920'] = image_data
            odoo_template.write(template_update_vals)
            # Don't update list_price for existing templates - it's managed by variants
          else:
            # For new products, set list_price to the MINIMUM variant price
            # This serves as the base price, and other variants will have price_extra
            min_price = 0.0
            if variants:
              try:
                # Get all variant prices and find the minimum
                variant_prices = [float(v.get('price', 0) or 0.0) for v in variants]
                min_price = min(variant_prices) if variant_prices else 0.0
              except (TypeError, ValueError):
                min_price = 0.0

            template_vals = {
                'name': shopify_product.get('title', 'Unknown Product'),
                'type': 'consu',
                'categ_id': self.env.ref('product.product_category_all').id,
                'description': shopify_product.get('body_html', '') or '',
                'list_price': min_price,  # Set to minimum variant price
                'taxes_id': [(5, 0, 0)],
                'supplier_taxes_id': [(5, 0, 0)],
                'is_storable': True,
                'shopify_external_id': product_id_str,  # Store Shopify product ID
            }
            if image_data:
              template_vals['image_1920'] = image_data
            if attribute_lines:
              template_vals['attribute_line_ids'] = attribute_lines
            odoo_template = ProductTemplate.create(template_vals)

          # Ensure template has all attribute lines/values from Shopify
          self._ensure_template_attribute_lines(odoo_template, attribute_info)
          odoo_template._create_variant_ids()

          ptav_map = self._build_ptav_map(odoo_template)
          mapping_by_variant = {
              mapping.shopify_variant_id: mapping for mapping in existing_mappings
          }

          processed_variant_ids = set()

          for shopify_variant in variants:
            variant_id_str = str(shopify_variant.get('id'))

            odoo_variant, variant_created = self._match_or_create_variant(
                odoo_template, shopify_variant, meaningful_options, attribute_info, ptav_map)

            self._update_variant_from_shopify(odoo_variant, shopify_variant)

            mapping_vals = {
                'name':
                    shopify_variant.get('title') or shopify_product.get('title', 'Unknown Product'),
                'shopify_product_id':
                    product_id_str,
                'shopify_variant_id':
                    variant_id_str,
                'odoo_product_id':
                    odoo_variant.id,
                'instance_id':
                    instance.id,
                'sync_status':
                    'synced',
                'last_sync':
                    fields.Datetime.now(),
                'sku':
                    shopify_variant.get('sku', ''),
                'stock_quantity':
                    float(shopify_variant.get('inventory_quantity', 0) or 0.0),
                'shopify_inventory_item_id':
                    str(shopify_variant.get('inventory_item_id', '') or ''),
                'product_color':
                    self._extract_color_from_variant(meaningful_options, shopify_variant)
                    or self._extract_color_from_options(shopify_product),
                'warehouse_location':
                    self._determine_warehouse_location(shopify_variant),
                'active':
                    True,
            }

            variant_mapping = mapping_by_variant.get(variant_id_str)

            if variant_mapping:
              variant_mapping.write(mapping_vals)
              if not variant_created:
                updated_count += 1
            else:
              try:
                _logger.info("Creating mapping for variant %s (product %s) on instance %s",
                             variant_id_str, odoo_variant.id, instance.id)
                variant_mapping = self.create(mapping_vals)
                mapping_by_variant[variant_id_str] = variant_mapping
                created_count += 1
              except ValidationError as create_error:
                # Likely unique constraint violation – fetch existing record and update it
                _logger.warning(
                    f"Mapping create failed for variant {variant_id_str}: {create_error}")
                variant_mapping = self.with_context(active_test=False).search(
                    [('shopify_variant_id', '=', variant_id_str),
                     ('instance_id', '=', instance.id)],
                    limit=1)
                if variant_mapping:
                  variant_mapping.write(mapping_vals)
                  mapping_by_variant[variant_id_str] = variant_mapping
                  updated_count += 1
                else:
                  error_count += 1
                  continue

            processed_variant_ids.add(variant_id_str)

          # Deactivate mappings not present anymore
          if existing_mappings:
            unused_mappings = existing_mappings.filtered(
                lambda m: m.shopify_variant_id and m.shopify_variant_id not in processed_variant_ids
            )
            if unused_mappings:
              unused_mappings.write({'active': False})

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
                  f"Error importing product {shopify_product.get('title', 'Unknown')}: {str(e)}",
          })

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

  def _get_meaningful_options(self, shopify_product):
    """Return Shopify options that represent real variants (skip default title)."""
    meaningful_options = []
    for idx, option in enumerate(shopify_product.get('options', []), start=1):
      name = (option.get('name') or '').strip()
      values = option.get('values') or []

      if not name:
        continue

      lower_name = name.lower()
      lower_values = [str(val or '').lower() for val in values]

      if lower_name == 'title' and (not values or all(val in ['default title', 'default']
                                                      for val in lower_values)):
        continue

      meaningful_options.append({
          'index': idx,
          'name': name,
          'values': values,
      })

    return meaningful_options

  def _prepare_attribute_data(self, meaningful_options):
    """Create or fetch product attributes/value records for Shopify options."""
    attribute_lines = []
    attribute_info = {}

    for option in meaningful_options:
      name = option['name']
      attribute = self.env['product.attribute'].search([('name', '=', name)], limit=1)
      if not attribute:
        attribute = self.env['product.attribute'].create({'name': name})

      value_map = {}
      value_ids = []
      for value in option.get('values', []):
        if not value:
          continue
        attribute_value = self.env['product.attribute.value'].search(
            [('name', '=', value), ('attribute_id', '=', attribute.id)], limit=1)
        if not attribute_value:
          attribute_value = self.env['product.attribute.value'].create({
              'name': value,
              'attribute_id': attribute.id,
          })
        value_map[value] = attribute_value
        value_ids.append(attribute_value.id)

      if value_ids:
        attribute_lines.append((0, 0, {
            'attribute_id': attribute.id,
            'value_ids': [(6, 0, value_ids)],
        }))

      attribute_info[name] = {
          'attribute': attribute,
          'value_map': value_map,
      }

    return attribute_lines, attribute_info

  def _ensure_template_attribute_lines(self, template, attribute_info):
    """Ensure the template contains all attribute lines/values required."""
    for option_name, data in attribute_info.items():
      attribute = data['attribute']
      value_ids = [value.id for value in data['value_map'].values() if value]
      if not value_ids:
        continue

      attribute_line = template.attribute_line_ids.filtered(
          lambda l: l.attribute_id.id == attribute.id)
      if attribute_line:
        existing_ids = attribute_line.value_ids.ids
        missing_ids = [vid for vid in value_ids if vid not in existing_ids]
        if missing_ids:
          attribute_line.write({'value_ids': [(4, vid) for vid in missing_ids]})
      else:
        template.write({
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attribute.id,
                'value_ids': [(6, 0, value_ids)],
            })]
        })

  def _build_ptav_map(self, template):
    """Return a mapping of attribute value IDs to product.template.attribute.value IDs."""
    ptav_records = self.env['product.template.attribute.value'].search([('product_tmpl_id', '=',
                                                                         template.id)])
    return {ptav.product_attribute_value_id.id: ptav.id for ptav in ptav_records}

  def _match_or_create_variant(self, template, shopify_variant, meaningful_options, attribute_info,
                               ptav_map):
    """Find or create the matching Odoo variant for a Shopify variant."""
    variant_id_str = str(shopify_variant.get('id'))

    # First, check if variant exists by Shopify external ID
    existing_variant = self.env['product.product'].search(
        [('shopify_external_id', '=', variant_id_str), ('product_tmpl_id', '=', template.id)],
        limit=1)

    if existing_variant:
      return existing_variant, False

    if not meaningful_options:
      variant = template.product_variant_ids[:1]
      if variant:
        # Update the external ID if it wasn't set
        if not variant.shopify_external_id:
          variant.write({'shopify_external_id': variant_id_str})
        return variant, False
      variant = self.env['product.product'].create({
          'product_tmpl_id': template.id,
          'default_code': shopify_variant.get('sku', ''),
          'barcode': shopify_variant.get('barcode', ''),
          'shopify_external_id': variant_id_str,
      })
      return variant, True

    desired_value_ids = []
    for option in meaningful_options:
      option_index = option['index']
      option_name = option['name']
      option_value = shopify_variant.get(f'option{option_index}')
      if not option_value:
        continue
      value_map = attribute_info.get(option_name, {}).get('value_map', {})
      attribute_value = value_map.get(option_value)
      if attribute_value:
        desired_value_ids.append(attribute_value.id)

    # Try to find an existing variant with the same combination
    for candidate in template.product_variant_ids:
      candidate_value_ids = candidate.product_template_attribute_value_ids.mapped(
          'product_attribute_value_id').ids
      if set(candidate_value_ids) == set(desired_value_ids):
        # Update the external ID if it wasn't set
        if not candidate.shopify_external_id:
          candidate.write({'shopify_external_id': variant_id_str})
        return candidate, False

    # Create new variant for the combination
    ptav_ids = []
    for attr_value_id in desired_value_ids:
      ptav_id = ptav_map.get(attr_value_id)
      if not ptav_id:
        ptav = self.env['product.template.attribute.value'].search(
            [('product_tmpl_id', '=', template.id),
             ('product_attribute_value_id', '=', attr_value_id)],
            limit=1)
        ptav_id = ptav.id if ptav else False
        if ptav_id:
          ptav_map[attr_value_id] = ptav_id
      if ptav_id:
        ptav_ids.append(ptav_id)

    variant_vals = {
        'product_tmpl_id': template.id,
        'shopify_external_id': variant_id_str,  # Store Shopify variant ID
    }
    if ptav_ids:
      variant_vals['product_template_attribute_value_ids'] = [(6, 0, ptav_ids)]

    variant = self.env['product.product'].create(variant_vals)
    return variant, True

  def _update_variant_from_shopify(self, variant, shopify_variant):
    """Write Shopify variant details onto the Odoo variant directly from Shopify data."""
    variant_id_str = str(shopify_variant.get('id'))
    sku = (shopify_variant.get('sku') or '').strip()
    barcode = (shopify_variant.get('barcode') or '').strip()
    available_quantity = shopify_variant.get('inventory_quantity', 0)

    try:
      variant_price = float(shopify_variant.get('price', 0) or 0.0)
    except (TypeError, ValueError):
      variant_price = 0.0

    template_base_price = variant.product_tmpl_id.list_price
    required_price_extra = variant_price - template_base_price

    update_vals = {
        'default_code': sku or False,
        'barcode': barcode or False,
        'taxes_id': [(5, 0, 0)],
        'supplier_taxes_id': [(5, 0, 0)],
        'shopify_external_id': variant_id_str,  # Always update external ID
    }

    weight_value = shopify_variant.get('weight')
    if weight_value is not None:
      try:
        update_vals['weight'] = float(weight_value)
      except (TypeError, ValueError):
        update_vals['weight'] = 0.0

    variant.write(update_vals)

    if available_quantity is not None:
      stock_location = self._get_default_stock_location()
      # Get current stock quantity
      stock_quant = self.env['stock.quant'].search([('product_id', '=', variant.id),
                                                    ('location_id', '=', stock_location.id)],
                                                   limit=1)
      current_qty = stock_quant.quantity if stock_quant else 0.0
      # Calculate the difference to update
      qty_difference = available_quantity - current_qty
      if qty_difference != 0:
        self.env['stock.quant']._update_available_quantity(variant, stock_location, qty_difference)

    if variant.product_template_attribute_value_ids and required_price_extra != 0:
      first_ptav = variant.product_template_attribute_value_ids[0]
      first_ptav.write({'price_extra': required_price_extra})

  def _extract_color_from_variant(self, meaningful_options, shopify_variant):
    """Extract the color value for a specific Shopify variant."""
    for option in meaningful_options:
      option_name = option['name'].lower()
      if option_name in ['color', 'colour', 'colorway']:
        value = shopify_variant.get(f"option{option['index']}")
        if value:
          return value
    return ''

  def _get_default_stock_location(self):
    location = self.env.ref('stock.stock_location_stock', raise_if_not_found=False)
    if location:
      return location
    return self.env['stock.location'].search([('usage', '=', 'internal'),
                                              ('company_id', 'in', [self.env.company.id, False])],
                                             limit=1)

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

  def _extract_color_from_options(self, shopify_product):
    """Extract color from product options"""
    try:
      options = shopify_product.get('options', [])
      for option in options:
        if option.get('name', '').lower() in ['color', 'colour', 'color']:
          values = option.get('values', [])
          if values:
            return values[0]  # Return first color

      # Try to get color from variant title
      variants = shopify_product.get('variants', [])
      if variants:
        variant_title = variants[0].get('title', '')
        # Common color keywords
        colors = [
            'red', 'blue', 'green', 'yellow', 'black', 'white', 'pink', 'purple', 'orange', 'brown',
            'gray', 'grey', 'silver', 'gold', 'beige', 'navy'
        ]
        for color in colors:
          if color in variant_title.lower():
            return color.capitalize()

      return ''
    except Exception as e:
      _logger.warning(f"Error extracting color: {str(e)}")
      return ''

  def _determine_warehouse_location(self, variant):
    """Determine warehouse location based on inventory management and fulfillment"""
    try:
      inventory_management = variant.get('inventory_management', '')
      fulfillment_service = variant.get('fulfillment_service', '')

      # Determine location based on fulfillment service
      if fulfillment_service in ['manual', 'shipwire', 'amazon_marketplace_web']:
        if fulfillment_service == 'amazon_marketplace_web':
          return 'dropship'
        elif inventory_management == 'shopify':
          # Check if there's inventory
          inventory_qty = variant.get('inventory_quantity', 0)
          if inventory_qty > 0:
            return 'warehouse'
          else:
            return 'online'
        else:
          return 'online'
      else:
        # Default to online for other cases
        return 'online'
    except Exception as e:
      _logger.warning(f"Error determining warehouse location: {str(e)}")
      return 'online'

  def sync_product_to_shopify(self, instance):
    """Bi-directional sync: Update product in Shopify with latest Odoo data"""
    self.ensure_one()

    if not self.shopify_product_id:
      _logger.warning(f"Cannot sync product {self.name} - no Shopify product ID")
      return False

    odoo_product = self.odoo_product_id

    # Prepare update data
    update_data = {
        'product': {
            'id':
                int(self.shopify_product_id),
            'title':
                odoo_product.name,
            'body_html':
                odoo_product.description or '',
            'status':
                'active' if odoo_product.active else 'draft',
            'variants': [{
                'id':
                    int(self.shopify_variant_id) if self.shopify_variant_id else None,
                'price':
                    str(odoo_product.list_price),
                'sku':
                    odoo_product.default_code or '',
                'inventory_quantity':
                    int(odoo_product.qty_available) if odoo_product.type == 'product' else 0,
            }]
        }
    }

    url = f"{instance.shop_url}/admin/api/2024-07/products/{self.shopify_product_id}.json"

    try:
      headers = {'Content-Type': 'application/json'}
      if hasattr(instance, 'access_token') and instance.access_token:
        headers['X-Shopify-Access-Token'] = instance.access_token
        response = requests.put(url, headers=headers, json=update_data, timeout=30)
      else:
        response = requests.put(url,
                                auth=(instance.api_key, instance.password),
                                json=update_data,
                                timeout=30)

      if response.status_code == 200:
        self.write({
            'sync_status': 'synced',
            'last_sync': fields.Datetime.now(),
        })
        _logger.info(f"Successfully synced product {self.name} to Shopify")
        return True
      else:
        _logger.error(
            f"Failed to sync product {self.name}: {response.status_code} - {response.text}")
        self.write({'sync_status': 'error'})
        return False
    except Exception as e:
      _logger.error(f"Exception syncing product {self.name}: {str(e)}")
      self.write({'sync_status': 'error'})
      return False

  # def sync_product_from_shopify(self, instance):
  #   """Bi-directional sync: Update Odoo product with latest Shopify data"""
  #   self.ensure_one()

  #   if not self.shopify_product_id:
  #     _logger.warning(f"Cannot sync product {self.name} - no Shopify product ID")
  #     return False

  #   url = f"{instance.shop_url}/admin/api/2024-07/products/{self.shopify_product_id}.json"

  #   try:
  #     headers = {'Content-Type': 'application/json'}
  #     if hasattr(instance, 'access_token') and instance.access_token:
  #       headers['X-Shopify-Access-Token'] = instance.access_token
  #       response = requests.get(url, headers=headers, timeout=20)
  #     else:
  #       response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

  #     if response.status_code == 200:
  #       shopify_product = response.json().get('product', {})
  #       variant = shopify_product.get('variants', [{}])[0]

  #       # Update Odoo product
  #       update_vals = {
  #           'name': shopify_product.get('title', self.odoo_product_id.name),
  #           'list_price': float(variant.get('price', 0)),
  #           'description': shopify_product.get('body_html', ''),
  #           'default_code': variant.get('sku', ''),
  #       }
  #       self.odoo_product_id.write(update_vals)

  #       # Update mapping fields
  #       mapping_vals = {
  #           'sku': variant.get('sku', ''),
  #           'stock_quantity': float(variant.get('inventory_quantity', 0)),
  #           'shopify_inventory_item_id': str(variant.get('inventory_item_id', '')),
  #           'product_color': self._extract_color_from_options(shopify_product),
  #           'warehouse_location': self._determine_warehouse_location(variant),
  #           'sync_status': 'synced',
  #           'last_sync': fields.Datetime.now(),
  #       }
  #       self.write(mapping_vals)

  #       _logger.info(f"Successfully synced product {self.name} from Shopify")
  #       return True
  #     else:
  #       _logger.error(f"Failed to fetch product {self.name} from Shopify: {response.status_code}")
  #       return False
  #   except Exception as e:
  #     _logger.error(f"Exception syncing product {self.name} from Shopify: {str(e)}")
  #     return False

  def action_sync_from_shopify(self):
    """Action to sync products from Shopify"""
    for product_mapping in self:
      instance = product_mapping.instance_id
      if instance:
        product_mapping.sync_product_from_shopify(instance)

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Synced {len(self)} product(s) from Shopify',
            'type': 'success',
            'sticky': False,
        },
    }

  def action_sync_to_shopify(self):
    """Action to sync products to Shopify"""
    for product_mapping in self:
      instance = product_mapping.instance_id
      if instance:
        product_mapping.sync_product_to_shopify(instance)

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Synced {len(self)} product(s) to Shopify',
            'type': 'success',
            'sticky': False,
        },
    }

  def fetch_inventory_levels_from_shopify(self, instance):
    """Fetch detailed inventory levels from Shopify for all products"""
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    # Get all product mappings for this instance
    product_mappings = self.search([('instance_id', '=', instance.id),
                                    ('shopify_inventory_item_id', '!=', False)])

    for mapping in product_mappings:
      try:
        if not mapping.shopify_inventory_item_id:
          continue

        # Fetch inventory levels for this inventory item
        url = f"{instance.shop_url}/admin/api/2024-07/inventory_levels.json?inventory_item_ids={mapping.shopify_inventory_item_id}"

        headers = {'Content-Type': 'application/json'}
        if hasattr(instance, 'access_token') and instance.access_token:
          headers['X-Shopify-Access-Token'] = instance.access_token
          response = requests.get(url, headers=headers, timeout=20)
        else:
          response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

        if response.status_code == 200:
          inventory_levels = response.json().get('inventory_levels', [])

          if inventory_levels:
            # Get the first location's inventory
            level = inventory_levels[0]
            mapping.write({
                'stock_quantity': float(level.get('available', 0)),
                'shopify_location_id': str(level.get('location_id', '')),
            })

            # Fetch location details to determine warehouse type
            if level.get('location_id'):
              self._fetch_location_details(instance, mapping, str(level.get('location_id')))
        else:
          _logger.warning(f"Failed to fetch inventory for {mapping.name}: {response.text}")

      except Exception as e:
        _logger.error(f"Error fetching inventory for {mapping.name}: {str(e)}")

  def _fetch_location_details(self, instance, mapping, location_id):
    """Fetch location details to determine if it's a warehouse, retail, etc."""
    try:
      url = f"{instance.shop_url}/admin/api/2024-07/locations/{location_id}.json"

      headers = {'Content-Type': 'application/json'}
      if hasattr(instance, 'access_token') and instance.access_token:
        headers['X-Shopify-Access-Token'] = instance.access_token
        response = requests.get(url, headers=headers, timeout=20)
      else:
        response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

      if response.status_code == 200:
        location = response.json().get('location', {})
        location_name = location.get('name', '').lower()

        # Determine warehouse location based on location name
        if 'warehouse' in location_name or 'fulfillment' in location_name:
          mapping.write({'warehouse_location': 'warehouse'})
        elif 'retail' in location_name or 'store' in location_name or 'shop' in location_name:
          mapping.write({'warehouse_location': 'retail'})
        elif 'dropship' in location_name:
          mapping.write({'warehouse_location': 'dropship'})
        else:
          mapping.write({'warehouse_location': 'online'})
    except Exception as e:
      _logger.warning(f"Error fetching location details: {str(e)}")
