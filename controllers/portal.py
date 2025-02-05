# # -*- coding: utf-8 -*-
import binascii
from datetime import date, datetime
from odoo import fields, http, _
from odoo.exceptions import AccessError, MissingError
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager, get_records_pager
from odoo.osv import expression
from odoo.exceptions import UserError, AccessError, ValidationError
from odoo.tools import ustr, consteq
import logging

_logger = logging.getLogger(__name__)


class CustomerPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super(CustomerPortal, self)._prepare_home_portal_values(counters)
        leaves_count = 0
        Employee = request.env['hr.employee']
        LeaveObj = request.env['hr.leave']
        employee = Employee.search([('user_id', '=', request.env.user.id)], limit=1)
        if employee:
            leaves = LeaveObj.search([('employee_id', '=', employee.name)])
            leaves_count = len(leaves)
        values.update({
            'leaves_count': leaves_count,
        })
        return values

    @http.route(['/my/leaves', '/my/leaves/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_leaves(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        values = self._prepare_portal_layout_values()
        partner = request.env.user.partner_id
        Employee = request.env['hr.employee']
        LeaveObj = request.env['hr.leave']
        employee = Employee.search([('user_id', '=', request.env.user.id)], limit=1)
        if not employee:
            return request.render("gts_hr_portal.leave_employee_not_found", values)
        domain = [('employee_id', '=', employee.id)]
        searchbar_sortings = {
            'date': {'label': _('Leave Date'), 'order': 'request_date_from desc'},
            'name': {'label': _('Leave Type'), 'order': 'holiday_status_id'},
            'stage': {'label': _('Stage'), 'order': 'state'},
        }
        if not sortby:
            sortby = 'date'
        sort_order = searchbar_sortings[sortby]['order']

        archive_groups = self._get_archive_groups('hr.leave', domain) if values.get('my_details') else []
        if date_begin and date_end:
            domain += [('create_date', '>', date_begin), ('create_date', '<=', date_end)]

        # count for pager
        leaves_count = LeaveObj.search_count(domain)
        # pager
        pager = portal_pager(
            url="/my/leaves",
            url_args={'date_begin': date_begin, 'date_end': date_end, 'sortby': sortby},
            total=leaves_count,
            page=page,
            step=self._items_per_page
        )
        # content according to pager and archive selected
        leaves = LeaveObj.search(domain, order=sort_order, limit=self._items_per_page, offset=pager['offset'])
        request.session['my_leaves_history'] = leaves.ids[:100]

        values.update({
            'date': date_begin,
            'leaves': leaves.sudo(),
            'page_name': 'leave',
            'pager': pager,
            'archive_groups': archive_groups,
            'default_url': '/my/leaves',
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
        })
        return request.render("gts_hr_portal.portal_my_leaves", values)

    @http.route(['/my/leaves/<int:leave_id>'], type='http', auth="public", website=True)
    def portal_leave_page(self, leave_id, report_type=None, access_token=None, message=False, download=False, **kw):
        try:
            leave_sudo = self._document_check_access('hr.leave', leave_id, access_token=access_token)
        except (AccessError, MissingError):
            return request.redirect('/my/leaves')

        values = {
            'leave': leave_sudo,
            'message': message,
            'token': access_token,
            'return_url': '/my/leaves',
            'bootstrap_formatting': True,
            'employee_id': leave_sudo.employee_id.id,
            'report_type': 'html',
        }

        history = request.session.get('my_leaves_history', [])
        values.update(get_records_pager(history, leave_sudo))

        return request.render('gts_hr_portal.view_my_hr_leave', values)

    @http.route(['/leave/apply'], type='http', auth="user", website=True)
    def leaves_apply(self, **kwargs):
        leave_types = request.env['hr.leave.type'].search([('request_unit', '=', 'day')])
        permission_types = request.env['hr.leave.type'].search([('request_unit', 'in', ('hour', 'half_day'))])
        employee = request.env['hr.employee'].search([('user_id', '=', request.env.user.id)], limit=1)
        if not employee:
            return request.render("gts_hr_portal.leave_employee_not_found", {})
        error = {}
        default = {}
        if 'website_hr_recruitment_error' in request.session:
            error = request.session.pop('website_hr_recruitment_error')
            default = request.session.pop('website_hr_recruitment_default')
        values = {
            'employee': employee,
            'employee_name': employee.name,
            'leave_types': leave_types,
            'permission_types': permission_types,
            'error': error,
            'default': default,
            'return_url': '/my/leaves',
            'bootstrap_formatting': True,
        }
        return request.render("gts_hr_portal.leave_apply", values)

    @http.route(["/leave/new/data"], type='http', auth="user", methods=['GET', 'POST'], website=True, csrf=False)
    def leave_create_new(self, **kwargs):
        values = kwargs
        today = fields.Date.today()
        _logger.info("Form submitted with data: %s", values)  # Log the incoming data
        Employee = request.env['hr.employee']
        LeaveObj = request.env['hr.leave']
        employee = Employee.search([('user_id', '=', request.env.user.id)], limit=1)
        values['validation_errors'] = {}
        leave_data = self._leave_cleanup_data(values)
        leave_data_copy = leave_data.copy()
        validation_errors = self._leave_new_validate_data(values)
        leave = False

        # Handle file upload
        attachment = request.httprequest.files.get('attachment')
        attachment_id = False
        if attachment:
            try:
                attachment_data = {
                    'name': attachment.filename,
                    'datas': binascii.b2a_base64(attachment.read()),
                    'res_model': 'hr.leave',
                    'type': 'binary',  # Ensure the type is set correctly
                }
                attachment_id = request.env['ir.attachment'].sudo().create(attachment_data).id
            except Exception as e:
                _logger.error("Failed to create attachment: %s", str(e))
                validation_errors.update(
                    {'attachment': {'error_text': _("Failed to upload attachment. Please try again.")}})

        if not validation_errors and request.httprequest.method == 'POST':
            try:
                # Prepare leave data
                request_date_from = datetime.strptime(leave_data_copy.get('request_date_from'), '%Y-%m-%d')
                if leave_data_copy.get('request_date_to'):
                    request_date_to = datetime.strptime(leave_data_copy.get('request_date_to'), '%Y-%m-%d')
                else:
                    request_date_to = request_date_from
                leave_data_copy['number_of_days'] = \
                LeaveObj._get_number_of_days(request_date_from, request_date_to, leave_data_copy.get('employee_id'))[
                    'days'] + 1

                # Check for overlapping leave requests
                overlapping_leaves = LeaveObj.search([
                    ('employee_id', '=', leave_data_copy.get('employee_id')),
                    ('state', 'in', ['confirm', 'validate']),  # Only check confirmed or validated leaves
                    ('request_date_from', '<=', leave_data_copy.get('request_date_to')),
                    ('request_date_to', '>=', leave_data_copy.get('request_date_from')),
                ])
                if overlapping_leaves:
                    raise ValidationError(
                        _("You cannot set two time off that overlap on the same day. Existing time off:\n%s") %
                        "\n".join(
                            [f"From {leave.request_date_from} To {leave.request_date_to} - {leave.state}" for leave in
                             overlapping_leaves]))

                # Check remaining leave balance
                leave_type = request.env['hr.leave.type'].browse(leave_data_copy.get('holiday_status_id'))
                remaining_leaves = leave_type.with_context(
                    employee_id=leave_data_copy.get('employee_id')).remaining_leaves
                if remaining_leaves < leave_data_copy['number_of_days']:
                    raise ValidationError(
                        _("The number of remaining time off is not sufficient for this time off type. Remaining: %s days") % remaining_leaves)

                # Log leave data before creation
                _logger.info("Creating leave with data: %s", leave_data_copy)

                # Create the leave record
                leave = LeaveObj.sudo().create(leave_data_copy)

                # Log success
                _logger.info("Leave created successfully: %s", leave.id)

                # Link the attachment to the leave record
                if attachment_id:
                    request.env['ir.attachment'].sudo().browse(attachment_id).write({'res_id': leave.id})

            except (UserError, AccessError, ValidationError) as exc:
                request.env.cr.rollback()
                _logger.error("Error while creating leave: %s", str(exc))
                validation_errors.update({'error': {'error_text': str(exc)}})
                values['validation_errors'] = validation_errors
                values = self._leave_get_default_data(employee, values)
            except Exception as e:
                request.env.cr.rollback()
                _logger.error("Unknown error while creating leave: %s", str(e))
                validation_errors.update(
                    {'error': {'error_text': _("Unknown server error. Please contact the administrator.")}})
                values['validation_errors'] = validation_errors
                values = self._leave_get_default_data(employee, values)
            else:
                # Redirect to the thank-you page if leave is created successfully
                return request.render("gts_hr_portal.leave_thankyou", {'employee': employee, 'leave': leave.sudo()})
        else:
            values['validation_errors'] = validation_errors
            values.update(leave_data)
            values = self._leave_get_default_data(employee, values)

        # Render the form again with errors if any
        return request.render("gts_hr_portal.leave_apply", values)

    def _leave_get_default_data(self, employee, values):
        leave_types = request.env['hr.leave.type'].search([('request_unit', '=', 'day')])
        permission_types = request.env['hr.leave.type'].search([('request_unit', 'in', ('hour', 'half_day'))])
        values.update({
            'employee': employee,
            'employee_name': employee.name,
            'leave_types': leave_types,
            'permission_types': permission_types,
        })
        return values

    def _leave_new_validate_data(self, post):
        errors = {}
        today = fields.Date.today()
        if post.get('request_date_from', False) and post.get('request_date_to', False):
            if post.get('request_date_from', False) > post.get('request_date_to', False):
                errors.update({'request_date_from': {'error_text': _("Date From must be less than Date To!")}})
        return errors

    def _leave_cleanup_data(self, values):
        # Remove invalid fields from the form data
        cleanup_columns = ['csrf_token', 'employee_name', 'validation_errors', 'leave_type', 'attachment[0][0]']
        for column_name in cleanup_columns:
            if column_name in values:
                values.pop(column_name)

        # Ensure required fields are properly formatted
        if values.get('employee_id'):
            values['employee_id'] = int(values.get('employee_id'))
        if values.get('holiday_status_id'):
            values['holiday_status_id'] = int(values.get('holiday_status_id'))
            if values.get('permission_status_id'):
                values.pop("permission_status_id")
        if values.get('permission_status_id'):
            values['holiday_status_id'] = int(values.get('permission_status_id'))
            values.pop("permission_status_id")
        if values.get('request_date_from'):
            values['date_from'] = values.get('request_date_from')
        if values.get('request_date_to'):
            values['date_to'] = values.get('request_date_to')
        if values.get('half_day'):
            values['request_unit_half'] = values.get('half_day')
        if values.get('custom_hours'):
            values['request_unit_hours'] = values.get('custom_hours')
        if values.get('request_hour_from'):
            values['request_hour_from'] = values.get('request_hour_from')
        if values.get('request_hour_to'):
            values['request_hour_to'] = values.get('request_hour_to')
        if values.get('request_date_from_period'):
            values['request_date_from_period'] = values.get('request_date_from_period')

        return values
    @http.route(['/get/employee/leaves/count/<int:employee_id>/<int:holiday_status_id>'],
                type='json', auth="public", methods=['POST'], website=True)
    def get_leaves_count(self, employee_id, holiday_status_id, **kw):
        print('employee_id, holiday_status_id....', employee_id, holiday_status_id)
        remaining_leaves = 0
        leave_type = request.env['hr.leave.type'].search([('id', '=', int(holiday_status_id))])
        if leave_type:
            employee = request.env['hr.employee'].search([('id', '=', int(employee_id))])
            if employee:
                remaining_leaves = leave_type.with_context(employee_id=employee_id).remaining_leaves
        return remaining_leaves

