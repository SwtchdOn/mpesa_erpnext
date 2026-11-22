from contextlib import contextmanager

import frappe


@contextmanager
def callback_job_user():
    """Run a job that a Safaricom callback queued as a permitted user.

    A background job runs as the user who queued it, and Safaricom's callback is
    Guest (its token is checked in api.callbacks). ERPNext checks permissions
    while a Payment Entry is built, validated and submitted (get_payment_entry,
    get_account_details, the accounts' currencies), whatever ignore_permissions
    says, so as Guest the payment can't be booked. Only then is the job run as
    Administrator, as Frappe's own scheduled jobs are: a person's job keeps
    their permissions. ERPNext still refuses Administrator a posting date in
    frozen accounts."""
    caller = frappe.session.user
    if caller != "Guest":
        yield
        return

    frappe.set_user("Administrator")
    try:
        yield
    finally:
        frappe.set_user(caller)
