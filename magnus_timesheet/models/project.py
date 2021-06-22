# -*- coding: utf-8 -*-
# Copyright 2018 Magnus ((www.magnus.nl).)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import models, fields, api, _, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError
from datetime import datetime


class Task(models.Model):
    _inherit = "project.task"

    @api.one
    @api.constrains('project_id', 'standard')
    def _check_project_standard(self):
        task = self.env['project.task'].search([('project_id', '=', self.project_id.id), ('standard', '=', True)])
        if len(task) > 1 and self.standard:
            raise ValidationError(_('You can have only one task with the standard as true per project!'))

    standard = fields.Boolean(
        string='Standard'
    )
    my_wiz_id = fields.Many2one(
        'my.wizard'
    )
    task_user_ids = fields.One2many(
        'task.user',
        'task_id',
        string='Can register time',
        track_visibility='always'
    )

    @api.model
    def name_search(self, name, args=None, operator='ilike', limit=100):
        args = args or []
        recs = self.browse()
        if name:
            recs = self.search([('name', '=', name)] + args, limit=limit)
        if not recs:
            recs = self.search(['|',('name', operator, name), ('jira_compound_key', operator, name)] + args, limit=limit)
        return recs.name_get()


class Project(models.Model):
    _inherit = "project.project"

    overtime = fields.Boolean(
        string='Overtime Taken'
    )
    overtime_hrs = fields.Boolean(
        string='Overtime Hours'
    )
    invoice_principle = fields.Selection(
        [
        ('ff', 'Fixed Fee'),
        ('tm', 'Time and Material'),
        ('ctm', 'Capped Time and Material')
    ], )
    invoice_schedule_ids = fields.One2many(
        'invoice.schedule.lines',
        'project_id',
        string='Invoice Schedule'
    )

    @api.one
    @api.constrains('overtime', 'overtime_hrs')
    def _check_project_overtime(self):
        company_id = self.company_id.id if self.company_id else False

        overtime_taken_project = self.search([('company_id', '=', company_id), ('overtime', '=', True)])
        if len(overtime_taken_project) > 1:
            raise ValidationError(_("You can have only one project with 'Overtime Taken' per company!"))

        overtime_project = self.search([('company_id', '=', company_id), ('overtime_hrs', '=', True)])
        if len(overtime_project) > 1:
            raise ValidationError(_("You can have only one project with 'Overtime Hours' per company!"))


class TaskUser(models.Model):
    _name = 'task.user'

    @api.one
    @api.depends('product_id')
    def _default_fee_rate(self):
        if self.product_id:
            self.fee_rate = self.product_id.list_price

    @api.model
    def _default_product(self):
        if self.user_id.employee_ids.product_id:
            return self.user_id.employee_ids.product_id.id

    @api.model
    def _get_category_domain(self):
        return [('categ_id', '=', self.env.ref(
            'magnus_timesheet.product_category_fee_rate').id)]

    task_id = fields.Many2one(
        'project.task',
        string='Task'
    )
    user_id = fields.Many2one(
        'res.users',
        string='Consultants'
    )
    product_id = fields.Many2one(
        'product.product',
        string='Fee rate Product',
        default=_default_product,
        domain=_get_category_domain
    )
    fee_rate = fields.Float(
        default=_default_fee_rate,
        string='Fee Rate',
    )

    from_date = fields.Date(
        string='From Date',
        default=datetime.today()
    )
    user_ids = fields.Many2many(
        'res.users',
        string='Consultants',
    )

    @api.onchange('user_id')
    def onchange_user_id(self):
        self.product_id = False
        self.fee_rate = 0
        if self.user_id:
            emp = self.env['hr.employee'].search([('user_id', '=', self.user_id.id)])
            if emp:
                product = emp.product_id
                self.product_id = product.id
                self.fee_rate = product.lst_price

    @api.onchange('user_ids')
    def onchange_users(self):
        if len(self.user_ids) == 1:
            self.user_id = self.user_ids.id
        else:
            self.user_id = self.user_ids and self.user_ids.ids[0] or False

    #update user_ids attribute if any user_id added already
    @api.model
    def update_user_ids(self):
        taskUser = self.sudo().search([])
        for tuser in taskUser:
            if tuser.user_id:
                tuser.user_ids = [(6, 0, [tuser.user_id.id])]

    @api.multi
    def get_task_user_obj(self, task_id, user_id, date):
        taskUserObj = self.search([
            ('from_date', '<=', date),
            ('task_id', '=', task_id),
            ('user_id', '=', user_id)
        ],
        order='from_date Desc', limit=1)
        return taskUserObj

    @api.multi
    def update_analytic_lines(self):
        next_fee_rate_date = self.search(
            [('from_date', '>', self.from_date),
             ('task_id', '=', self.task_id.id),
             ('user_id', '=', self.user_id.id)],
            order='from_date', limit=1)

        aal_obj = self.env['account.analytic.line']
        aal_domain = [
            ('task_id', '=', self.task_id.id),
            ('user_id', '=', self.user_id.id),
            ('state', 'not in', ['invoiced','invoiced-by-fixed','write_off','expense-invoiced']),
            ('date', '>=', self.from_date)
        ]

        if next_fee_rate_date:
            aal_domain += [('date', '<', next_fee_rate_date.from_date)]
        aal_query_line = aal_obj._where_calc(aal_domain)
        aal_tables, aal_where_clause, aal_where_clause_params = aal_query_line.get_sql()

        list_query = ("""
                WITH aal AS (
                    SELECT
                       id, unit_amount
                    FROM
                       {0}
                    WHERE {1}
                )
                UPDATE {0} SET line_fee_rate = {2}, amount = (- aal.unit_amount * {2})
                FROM aal WHERE {0}.id = aal.id
                        """.format(
            aal_tables,
            aal_where_clause,
            self.fee_rate
        ))
        self.env.cr.execute(list_query, aal_where_clause_params)
        return True

    @api.one
    def split_task_users(self):
        if len(self.user_ids) == 1:
            return
        data = {'fee_rate': self.fee_rate, 'from_date': self.from_date,
                'product_id': self.product_id.id, 'task_id':self.task_id.id}
        for user in (self.user_ids - self.user_id):
            tUser = self.search([('user_id', '=', user.id), ('task_id', '=', self.task_id.id),
                                 ('from_date', '=', self.from_date),
                                 ('product_id', '=', self.product_id and self.product_id.id or False),])
            if tUser:
                tUser.fee_rate = self.fee_rate
                tUser.user_ids = [(6, 0, [user.id])]
            else:
                data['user_id'] = user.id
                data['user_ids'] = [(6, 0, [user.id])]
                self.create(data)
        self.user_ids = [(6, 0, [self.user_id.id])]

    @api.model
    def create(self, vals):
        res = super(TaskUser, self).create(vals)
        res.split_task_users()
        res.update_analytic_lines()
        return res

    @api.multi
    def write(self, vals):
        result = super(TaskUser, self).write(vals)
        self.split_task_users()
        for res in self:
            res.update_analytic_lines()
        return result

class InvoiceScheduleLine(models.Model):
    _name = 'invoice.schedule.lines'

    project_id = fields.Many2one(
        'project.project',
    )


class ProjectInvoicingProperties(models.Model):
    _inherit = "project.invoicing.properties"

    invoice_mileage = fields.Boolean(
        'Invoice Mileage'
    )
    group_invoice = fields.Boolean(
        'Group Invoice'
    )
    group_by_fee_rate = fields.Boolean(
        'Group By Fee Rate'
    )
    group_by_month = fields.Boolean(
        'Group By Month'
    )

    @api.onchange('invoice_mileage')
    def onchange_invoice_mileage(self):
        try:
            id = self._origin.id
        except:
            id = self.id
        project = self.env['project.project'].search([('invoice_properties', '=', id)])
        if project:
            analytic_lines = self.env['account.analytic.line'].search([
                ('project_id', 'in', project.ids),
                ('product_uom_id', '=', self.env.ref('product.product_uom_km').id)
            ])
            if analytic_lines:
                non_invoiceable_mileage = False if self.invoice_mileage else True
                cond = '='
                rec = analytic_lines.ids[0]
                if len(analytic_lines) > 1:
                    cond = 'IN'
                    rec = tuple(analytic_lines.ids)
                self.env.cr.execute("""
                    UPDATE account_analytic_line SET product_uom_id = %s, non_invoiceable_mileage = %s WHERE id %s %s
                """ % (self.env.ref('product.product_uom_km').id, non_invoiceable_mileage, cond, rec))