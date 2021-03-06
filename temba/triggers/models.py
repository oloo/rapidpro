from __future__ import unicode_literals

import re

from django.db import models
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from smartmin.models import SmartModel
from temba.contacts.models import Contact, ContactGroup
from temba.orgs.models import Org
from temba.channels.models import Channel
from temba.flows.models import Flow, FlowRun
from temba.msgs.models import Msg, Call
from temba.ivr.models import IVRCall

KEYWORD_TRIGGER = 'K'
SCHEDULE_TRIGGER = 'S'
MISSED_CALL_TRIGGER = 'M'
CATCH_ALL_TRIGGER = 'C'
FOLLOW_TRIGGER = 'F'
INBOUND_CALL_TRIGGER = 'V'

TRIGGER_TYPES = ((KEYWORD_TRIGGER, _("Keyword Trigger")),
                 (SCHEDULE_TRIGGER, _("Schedule Trigger")),
                 (INBOUND_CALL_TRIGGER, _("Inbound Call Trigger")),
                 (MISSED_CALL_TRIGGER, _("Missed Call Trigger")),
                 (CATCH_ALL_TRIGGER, _("Catch All Trigger")),
                 (FOLLOW_TRIGGER, _("Follow Account Trigger")))


class Trigger(SmartModel):

    """
    A Trigger is used to start a user in a flow based on an event. For example, triggers might fire
    for missed calls, inboud sms messages starting with a keyword, or on a repeating schedule.
    """

    org = models.ForeignKey(Org, verbose_name=_("Org"), help_text=_("The organization this trigger belongs to"))

    keyword = models.CharField(verbose_name=_("Keyword"), max_length=16, null=True, blank=True,
                               help_text=_("The first word in the message text"))

    flow = models.ForeignKey(Flow, verbose_name=_("Flow"), null=True, blank=True,
                             help_text=_("Which flow will be started"), related_name="triggers")

    last_triggered = models.DateTimeField(verbose_name=_("Last Triggered"), default=None, null=True,
                                          help_text=_("The last time this trigger was fired"))

    trigger_count = models.IntegerField(verbose_name=_("Trigger Count"), default=0,
                                        help_text=_("How many times this trigger has fired"))

    is_archived = models.BooleanField(verbose_name=_("Is Archived"), default=False,
                                      help_text=_("Whether this trigger is archived"))

    groups = models.ManyToManyField(ContactGroup, verbose_name=_("Groups"),
                                    help_text=_("The groups to broadcast the flow to"))

    contacts = models.ManyToManyField(Contact, verbose_name=_("Contacts"),
                                      help_text=_("Individual contacts to broadcast the flow to"))

    schedule = models.OneToOneField('schedules.Schedule', verbose_name=_("Schedule"),
                                    null=True, blank=True, related_name='trigger',
                                    help_text=_('Our recurring schedule'))

    trigger_type = models.CharField(max_length=1, choices=TRIGGER_TYPES, default=KEYWORD_TRIGGER, verbose_name=_("Trigger Type"),
                                    help_text=_('The type of this trigger'))

    channel = models.OneToOneField(Channel, verbose_name=_("Channel"), null=True, help_text=_("The associated channel"))

    def __unicode__(self):
        if self.trigger_type == KEYWORD_TRIGGER:
            return self.keyword
        return self.get_trigger_type_display()

    def name(self):
        return self.__unicode__()

    def as_json(self):
        """
        An exportable dict representing our trigger
        """
        return dict(trigger_type=self.trigger_type,
                    keyword=self.keyword,
                    flow=dict(id=self.flow.pk, name=self.flow.name),
                    groups=[dict(id=group.pk, name=group.name) for group in self.groups.all()],
                    channel=self.channel.pk if self.channel else None)

    @classmethod
    def import_triggers(cls, exported_json, org, user, site=None):
        """
        Import triggers from our export file
        """
        from temba.orgs.models import EARLIEST_IMPORT_VERSION
        if exported_json.get('version', 0) < EARLIEST_IMPORT_VERSION:
            raise ValueError(_("Unknown version (%s)" % exported_json.get('version', 0)))

        # first things first, let's create our groups if necesary and map their ids accordingly
        if 'triggers' in exported_json:
            for trigger_spec in exported_json['triggers']:

                # resolve our groups
                groups = []
                for group_spec in trigger_spec['groups']:

                    group = None

                    if site and site == exported_json.get('site', None):
                        group = ContactGroup.objects.filter(org=org, pk=group_spec['id']).first()

                    if not group:
                        group = ContactGroup.objects.filter(org=org, name=group_spec['name']).first()

                    if not group:
                        group = ContactGroup.create(org, user, group_spec['name'])

                    if not group.is_active:
                        group.is_active = True
                        group.save()

                    groups.append(group)

                flow = Flow.objects.get(org=org, pk=trigger_spec['flow']['id'])

                # see if that trigger already exists
                trigger = Trigger.objects.filter(org=org, trigger_type=trigger_spec['trigger_type'])

                if trigger_spec['keyword']:
                    trigger = trigger.filter(keyword__iexact=trigger_spec['keyword'])

                if groups:
                    trigger = trigger.filter(groups__in=groups)

                trigger = trigger.first()

                channel = trigger_spec.get('channel', None)  # older exports won't have a channel

                if trigger:
                    trigger.is_archived = False
                    trigger.flow = flow
                    trigger.save()
                else:
                    trigger = Trigger.objects.create(org=org, trigger_type=trigger_spec['trigger_type'],
                                                     keyword=trigger_spec['keyword'], flow=flow,
                                                     created_by=user, modified_by=user,
                                                     channel=channel)

                    for group in groups:
                        trigger.groups.add(group)


    @classmethod
    def get_triggers_of_type(cls, org, trigger_type):
        return Trigger.objects.filter(org=org, trigger_type=trigger_type, is_active=True, is_archived=False)

    @classmethod
    def catch_triggers(cls, entity, trigger_type, channel_id=None):
        if isinstance(entity, Msg):
            contact = entity.contact
            start_msg = entity
        elif isinstance(entity, Call) or isinstance(entity, IVRCall):
            contact = entity.contact
            start_msg = None
        elif isinstance(entity, Contact):
            contact = entity
            start_msg = None
        else:
            raise ValueError("Entity must be of type msg, call or contact")

        triggers = Trigger.get_triggers_of_type(entity.org, trigger_type)

        if channel_id:
            triggers = triggers.filter(channel_id=channel_id)

        for trigger in triggers:
            trigger.flow.start([], [contact], start_msg=start_msg, restart_participants=True)

        return bool(triggers)

    @classmethod
    def find_and_handle(cls, msg):
        # get the first word out of our message
        words = re.split(r"[\W]+", msg.text.strip(), flags=re.UNICODE)

        while words and not words[0]:
            words = words[1:]

        if not words:
            return False

        keyword = words[0].lower()

        if not keyword:
            return False

        active_run = FlowRun.objects.filter(is_active=True, contact=msg.contact, flow__is_active=True,
                                            flow__is_archived=False).order_by("-created_on", "-pk").first()

        if active_run and active_run.flow.ignore_triggers and not active_run.is_completed():
            return False

        groups_ids = msg.contact.groups.values_list('pk', flat=True)

        # Check first if we have a trigger for the contact groups
        matching = Trigger.objects.filter(is_archived=False, is_active=True, org=msg.org, keyword__iexact=keyword,
                                          flow__is_archived=False, flow__is_active=True, groups__in=groups_ids).order_by('groups__name').prefetch_related('groups', 'groups__contacts')

        # If no trigger for contact groups find there is a no group trigger
        if not matching:
            matching = Trigger.objects.filter(is_archived=False, is_active=True, org=msg.org, keyword__iexact=keyword,
                                              flow__is_archived=False, flow__is_active=True, groups=None).prefetch_related('groups', 'groups__contacts')

        if not matching:
            return False

        trigger = matching[0]

        trigger.last_triggered = msg.created_on
        trigger.trigger_count += 1
        trigger.save()

        contact = msg.contact

        # if we have an associated flow, start this contact in it
        trigger.flow.start([], [contact], start_msg=msg, restart_participants=True)

        return True

    @classmethod
    def find_flow_for_inbound_call(cls, contact):

        groups_ids = contact.groups.values_list('pk', flat=True)

        # Check first if we have a trigger for the contact groups
        matching = Trigger.objects.filter(is_archived=False, is_active=True, org=contact.org, trigger_type=INBOUND_CALL_TRIGGER,
                                          flow__is_archived=False, flow__is_active=True, groups__in=groups_ids).order_by('groups__name').prefetch_related('groups', 'groups__contacts')

        # If no trigger for contact groups find there is a no group trigger
        if not matching:
            matching = Trigger.objects.filter(is_archived=False, is_active=True, org=contact.org, trigger_type=INBOUND_CALL_TRIGGER,
                                              flow__is_archived=False, flow__is_active=True, groups=None).prefetch_related('groups', 'groups__contacts')

        if not matching:
            return None

        trigger = matching[0]
        trigger.last_triggered = timezone.now()
        trigger.trigger_count += 1
        trigger.save()

        return trigger.flow

    @classmethod
    def apply_action_archive(cls, triggers):
        triggers.update(is_archived=True)
        return [each_trigger.pk for each_trigger in triggers]

    @classmethod
    def apply_action_restore(cls, triggers):
        m_last_triggered = triggers.filter(trigger_type=MISSED_CALL_TRIGGER).order_by('-last_triggered', '-modified_on')
        c_last_triggered = triggers.filter(trigger_type=CATCH_ALL_TRIGGER).order_by('-last_triggered', '-modified_on')

        remaining_triggers = triggers.exclude(pk__in=m_last_triggered).exclude(pk__in=c_last_triggered)

        for trigger in remaining_triggers:

            if trigger.keyword:
                same_keyword_triggers = Trigger.objects.filter(org=trigger.org, keyword=trigger.keyword, is_archived=False, is_active=True,
                                                               trigger_type=KEYWORD_TRIGGER)
                if same_keyword_triggers:
                    same_keyword_triggers.update(is_archived=True)

            trigger.is_archived = False
            trigger.save()

        if m_last_triggered:
            # first archive all our missed call triggers and unarchive the last triggered in the selected
            Trigger.objects.filter(org=m_last_triggered[0].org,
                                   trigger_type=MISSED_CALL_TRIGGER,
                                   is_active=True).update(is_archived=True)
            m_last_triggered[0].is_archived = False
            m_last_triggered[0].save()

        if c_last_triggered:
            # first archive all our catch all message triggers and unarchive the last triggered in the selected
            Trigger.objects.filter(org=c_last_triggered[0].org,
                                   trigger_type=CATCH_ALL_TRIGGER,
                                   is_active=True).update(is_archived=True)
            c_last_triggered[0].is_archived = False
            c_last_triggered[0].save()

        return [each_trigger.pk for each_trigger in triggers]

    def fire(self):
        if self.is_archived or not self.is_active:
            return None

        channels = self.flow.org.channels.all()
        if not channels:
            return None

        groups = list(self.groups.all())
        contacts = list(self.contacts.all())

        if groups or contacts:
            self.last_triggered = timezone.now()
            self.trigger_count += 1
            self.save()

            return self.flow.start(groups, contacts, restart_participants=True) 

        return False
