from datetime import datetime, timedelta
from celery.task import periodic_task, task
from corehq.apps.reminders.models import (CaseReminderHandler, CaseReminder,
    CASE_CRITERIA)
from django.conf import settings
from dimagi.utils.logging import notify_exception
from casexml.apps.case.models import CommCareCase
from dimagi.utils.couch import CriticalSection

# In minutes
CASE_CHANGED_RETRY_INTERVAL = 5
CASE_CHANGED_RETRY_MAX = 3
CELERY_REMINDERS_QUEUE = "reminder_queue"

if not settings.REMINDERS_QUEUE_ENABLED:
    @periodic_task(run_every=timedelta(minutes=1),
        queue=settings.CELERY_PERIODIC_QUEUE)
    def fire_reminders():
        CaseReminderHandler.fire_reminders()

def get_subcases(case):
    indices = case.reverse_indices
    subcases = []
    for index in indices:
        if index.identifier == "parent":
            subcases.append(CommCareCase.get(index.referenced_id))
    return subcases

@task(queue=settings.CELERY_REMINDER_CASE_UPDATE_QUEUE)
def case_changed(case_id, handler_ids, retry_num=0):
    try:
        _case_changed(case_id, handler_ids)
    except Exception:
        if retry_num <= CASE_CHANGED_RETRY_MAX:
            try:
                # Try running the rule update again
                case_changed.apply_async(args=[case_id, handler_ids],
                    kwargs={"retry_num": retry_num + 1},
                    countdown=(60*CASE_CHANGED_RETRY_INTERVAL))
            except Exception:
                notify_exception(None,
                    message="Error processing reminder rule updates for case %s" %
                    case_id)
        else:
            notify_exception(None,
                message="Error processing reminder rule updates for case %s" %
                case_id)

def _case_changed(case_id, handler_ids):
    subcases = None
    case = CommCareCase.get(case_id)
    for handler_id in handler_ids:
        handler = CaseReminderHandler.get(handler_id)
        if handler.start_condition_type == CASE_CRITERIA:
            handler.case_changed(case)
            if handler.uses_parent_case_property:
                if subcases is None:
                    subcases = get_subcases(case)
                for subcase in subcases:
                    handler.case_changed(subcase)

@task(queue=settings.CELERY_REMINDER_RULE_QUEUE)
def process_reminder_rule(handler, schedule_changed, prev_definition,
    send_immediately):
    try:
        handler.process_rule(schedule_changed, prev_definition, send_immediately)
    except Exception:
        notify_exception(None,
            message="Error processing reminder rule for handler %s" % handler._id)
    handler.save(unlock=True)

@task(queue=CELERY_REMINDERS_QUEUE)
def fire_reminder(reminder_id):
    try:
        _fire_reminder(reminder_id)
    except Exception:
        notify_exception(None,
            message="Error firing reminder %s" % reminder_id)

def reminder_is_stale(reminder, utcnow):
    delta = timedelta(hours=settings.REMINDERS_QUEUE_STALE_REMINDER_DURATION)
    return (utcnow - delta) > reminder.next_fire

def _fire_reminder(reminder_id):
    utcnow = datetime.utcnow()
    reminder = CaseReminder.get(reminder_id)
    # This key prevents doc update conflicts with rule running
    key = "rule-update-definition-%s-case-%s" % (reminder.handler_id, reminder.case_id)
    with CriticalSection([key],
        timeout=(settings.REMINDERS_QUEUE_PROCESSING_LOCK_TIMEOUT*60)):
        # Refresh the reminder
        reminder = CaseReminder.get(reminder_id)
        if (not reminder.retired and reminder.active
            and utcnow >= reminder.next_fire):
            handler = reminder.handler
            if reminder_is_stale(reminder, utcnow):
                handler.set_next_fire(reminder, utcnow)
                reminder.save()
                return
            if handler.fire(reminder):
                handler.set_next_fire(reminder, utcnow)
                reminder.save()


