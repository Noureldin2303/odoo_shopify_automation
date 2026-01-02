from odoo import models, fields, api
import requests
import logging
import time
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
      index=True,
      copy=False,
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
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    # Prepare list of product batches (each batch is a list of product dicts).
    product_batches = []
    since_id = 6643732938904
    try:
      next_url = f"{instance.shop_url}/admin/api/2025-10/products.json?limit=3&since_id={since_id or ''}"

      while next_url:
        if hasattr(instance, 'access_token') and instance.access_token:
          headers = {
              'X-Shopify-Access-Token': instance.access_token,
              'Content-Type': 'application/json'
          }
          response = requests.get(next_url, headers=headers, timeout=30)
        else:
          response = requests.get(next_url, auth=(instance.api_key, instance.password), timeout=30)
        if response.status_code != 200:
          _logger.error(
              f"Product import failed with status {response.status_code}: {response.text}")
          raise UserError(_(f'Failed to import products - HTTP {response.status_code}: {response.text}'))

        data = response.json()
        batch = data.get('products', []) or []
        product_batches.append(batch)

        if not batch:
          break

        last_product = batch[-1]
        since_id = last_product.get('id')
        # if since_id:
        #   next_url = f"{instance.shop_url}/admin/api/2025-10/products.json?limit=10&since_id={since_id}"
        # else:
        next_url = None
        time.sleep(1)  # Sleep to avoid rate limits

    except Exception as e:
      raise UserError(_(f'Exception during product import: {str(e)}'))

    created_count = 0
    updated_count = 0
    error_count = 0

    ProductTemplate = self.env['product.template']

    # Iterate batches to process 100 products at a time
    for batch in product_batches:
      for shopify_product in batch:
        try:
          existing_template = False
          product_id_str = str(shopify_product.get('id'))
          if isinstance(product_id_str, str) and '/' in product_id_str:
            product_id_str = product_id_str.split('/')[-1]

          # check for product.template if exists then skip
          existing_template = self.env['product.template'].search(
              [('shopify_external_id', '=', product_id_str)], limit=1)

          if not existing_template:
            existing_template = self.env['product.template'].search([
                ('shopify_external_id', '=', False),
                ('name', '=', shopify_product.get('title')),
            ],
                                                                    limit=1)
          if existing_template and not existing_template.shopify_external_id:
            self.env['product.template'].browse(existing_template.id).write(
                {'shopify_external_id': product_id_str})

          variants = shopify_product.get('variants', [])
          if isinstance(variants, dict):
            variants = variants.get('nodes', []) or []

          if not variants:
            continue

          for shopify_variant in variants:
            existing_variant = False
            variant_id_str = str(shopify_variant.get('id'))

            existing_variant = self.env['product.product'].search(
                [('shopify_external_id', '=', variant_id_str)], limit=1)

            if not existing_variant:
              existing_variant = self.env['product.product'].search([
                  ('shopify_product_external_id', '=', product_id_str),
              ],
                                                                    limit=1)

            if not existing_variant:
              existing_variant = self.env['product.product'].search([
                  '|',
                  ('default_code', '=', shopify_variant.get('sku', '')),
                  ('name', '=', shopify_product.get('title')),
              ],
                                                                    limit=1)
            if existing_variant and (not existing_variant.shopify_external_id or
                                     not existing_variant.shopify_product_external_id):
              self.env['product.product'].browse(existing_variant.id).write({
                  'shopify_external_id': variant_id_str,
                  'shopify_product_external_id': product_id_str,
              })

            if existing_variant and (existing_variant.shopify_external_id or
                                     existing_variant.shopify_product_external_id):
              continue
            else:
              _logger.exception("Product variant doesn't mapped name=%s,id=%s",
                                shopify_variant.get('title'), shopify_variant.get('id'))
              continue

        except Exception as e:
          error_count += 1
          _logger.exception('Error processing Shopify product %s: %s',
                            shopify_product.get('id') if shopify_product else 'unknown', str(e))
          try:
            # Rollback to clear failed transaction so further DB ops can continue
            self.env.cr.rollback()
          except Exception:
            _logger.exception('Failed to rollback after exception')

      # Commit and sleep between batches to avoid long-running DB transactions
      try:
        self.env.cr.commit()
      except Exception:
        _logger.exception('Failed to commit DB after processing batch')

      # Sleep to avoid endpoint rate/timeouts and relieve DB
      try:
        time.sleep(getattr(instance, 'import_batch_sleep_seconds', 1))
      except Exception:
        pass

    # Return flattened list of fetched products (all batches)
    products = [p for batch in product_batches for p in batch]
    return products

  @api.model
  def _run_product_import_cron(self):
    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])
    for instance in instances:
      self.import_products_from_shopify(instance)

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
        'default_code': shopify_variant.get('sku', ''),
        'barcode': shopify_variant.get('barcode', ''),
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
        url = f"{instance.shop_url}/admin/api/2024-10/inventory_levels.json?inventory_item_ids={mapping.shopify_inventory_item_id}"

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
      url = f"{instance.shop_url}/admin/api/2024-10/locations/{location_id}.json"

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
