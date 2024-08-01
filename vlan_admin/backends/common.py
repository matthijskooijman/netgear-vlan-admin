import collections
from urwid import MetaSignals, emit_signal
import time


class CommitException(Exception):
    pass


class Switch(metaclass=MetaSignals):
    signals = ['changelist_changed', 'details_changed', 'portlist_changed', 'vlanlist_changed', 'status_changed']

    def __init__(self, config=None):
        self.ports = []
        self.vlans = []
        self.dotq_vlans = {}
        self.config = config
        self.changes = []
        self.max_vlan_internal_id = 0

        for column in self.switch_attrs:
            for (label_text, attr, edit) in column:
                setattr(self, attr, None)

        super().__init__()

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        emit_signal(self, name, self, *args)

    def add_vlan(self, dotq_id):
        vlan = Vlan(self, None, dotq_id, '')
        for port in self.ports:
            vlan.ports[port] = Vlan.NOTMEMBER

        self.vlans.append(vlan)
        self.dotq_vlans[dotq_id] = vlan

        self.queue_change(AddVlanChange(vlan, None, None))

        self._emit('vlanlist_changed')

    def delete_vlan(self, vlan):
        # Delete the vlan from the lists
        self.vlans.remove(vlan)
        del self.dotq_vlans[vlan.dotq_id]

        self.queue_change(DeleteVlanChange(vlan, None, None))

        self._emit('vlanlist_changed')

    def queue_change(self, new_change):
        # Make a new changes list to prevent issues with inline
        # modification (while looping the list)
        new_changes = []
        for change in reversed(self.changes):
            if new_change:
                # As long as the new_change hasn't removed itself yet,
                # try to merge it with each change
                (new_change, changes) = new_change.merge_with(change)

                # And replace the merged-with change with new one(s)
                # returned (or effectively remove it if an empty list is
                # returned).
                new_changes.extend(reversed(changes))
            else:
                # If the new change has already cancelled itself, just
                # preserve the remaining changes.
                new_changes.append(change)

        new_changes.reverse()

        if new_change:
            new_changes.append(new_change)

        # Update the changes list
        self.changes = new_changes
        # Always call this, just in case something changed
        self._emit('changelist_changed')

    def do_login(self):
        # Default to no login needed
        pass

    def do_logout(self):
        # Default to no logout needed
        pass

    def commit_all(self):
        """
        Commit all pending changes.

        This method decides the order in which to commit changes. This
        order was originally written for the FS726T, which is a bit
        picky about PVID changes (PVID must always point to a vlan the
        port is member of), but this order probably works well with
        other switches too.
        """

        def changes_of_type(type):
            return [c for c in self.changes if isinstance(c, type)]

        if not self.changes:
            raise CommitException("No changes to commit")

        self._emit('status_changed', "Committing changes...")

        # Maps a vlan to a dict that maps port to (newvalue, oldvalue)
        # tuples. Undefined elements default to the empty dict.
        memberships = collections.defaultdict(dict)

        # Maps vlans to a list of ports that must be committed before
        # repsectively after setting the PVIDs. Any vlan/port
        # combinations not listed in these can be committed at any time.
        first_pass = collections.defaultdict(list)
        second_pass = collections.defaultdict(list)

        # Keep a list of vlans to delete
        delete_vlans = []

        for change in self.changes:
            if isinstance(change, PortDescriptionChange):
                self.commit_port_description_change(change.what, change.how)
            elif isinstance(change, VlanNameChange):
                self.commit_vlan_description_change(change.what, change.how)
            elif isinstance(change, PortPVIDChange):
                # This port must be added to the vlan new PVID in the
                # first pass (before setting the PVIDs)
                first_pass[self.dotq_vlans[change.how]].append(change.what)
                # This port must be removed to the vlan new PVID in the
                # second pass (after setting the PVIDs). Don't bother if
                # the vlan is not in dotq_vlans (i.e. is deleted)
                if change.old in self.dotq_vlans:
                    second_pass[self.dotq_vlans[change.old]].append(change.what)
            elif isinstance(change, PortVlanMembershipChange):
                memberships[change.vlan][change.port] = (change.how, change.old)
            elif isinstance(change, AddVlanChange):
                # Make sure that the vlan has an entry in memberships,
                # even if no ports need changing.
                memberships[change.what]
            elif isinstance(change, DeleteVlanChange):
                delete_vlans.append(change.what)
            else:
                assert False, "Unknown change type? (%s)" % (type(change))

        def commit_memberships(vlan, ports):
            """
            Helper function to commit the changes in the given vlan (for
            the given ports only).
            """
            changes = memberships[vlan]
            changelist = []

            # Find the new (or old) value for each of the ports
            for port in self.ports:
                if port not in changes:
                    # Just use the old (unmodified) value
                    changelist.append(vlan.ports[port])
                else:
                    (new, old) = changes[port]

                    if port not in ports:
                        # Don't commit this value yet, use the old value
                        changelist.append(old)
                    else:
                        # Commit the new value
                        changelist.append(new)

            self.commit_vlan_memberships(vlan, changelist)

        # Committing vlan memberships happens in two passes: First, we
        # commit all vlan/port combinations that have to happen before
        # changing the PVIDs. For efficiency reasons, we commit all
        # ports for a given vlan if possible (i.e., when no ports have
        # to wait until the second pass). Then, we commit all the new
        # PVIDs. Finally, we do a second pass, committing any remaining
        # vlan/port membership changes. This is needed, since you can't
        # change the PVID of a port to a vlan it's not a member of (and
        # conversely, you can't remove a port from a vlan that's set as
        # its PVID).
        for vlan, ports in first_pass.items():
            if vlan in second_pass:
                # If we run this vlan again in the second pass, just
                # commit the ports that really need to be before the
                # PVID changes
                commit_memberships(vlan, ports)
            else:
                # If we don't need to run this vlan again in the second
                # pass, just commit all ports.
                commit_memberships(vlan, self.ports)
                del memberships[vlan]

        # If any pvids should be changed, commit the current (new)
        # values of all PVIDs. No need to actually look at the
        # PortPVIDChange objects generated, since we can only set all of
        # the PVIDs in a single request.
        if first_pass or second_pass:
            self.commit_pvids([p.pvid for p in self.ports])

        # And now, the second pass, just commit any remaining changes
        for vlan in memberships:
            commit_memberships(vlan, self.ports)

        # Finally, we delete the vlans that need to be deleted. We wait
        # until after the membership changes since 1) deletion can only
        # happen after updating PVIDs and 2) deletion messes with the
        # internal_ids, so it's a lot easier to do them all in go an
        # then renumber the remaining vlans.
        for vlan in delete_vlans:
            self.commit_vlan_delete(vlan)

        # Renumber the remaining vlans (just like the switch does
        # internally).
        # TODO: This should probably be moved to FS726T subclass
        if delete_vlans:
            for i in range(0, len(self.vlans)):
                self.vlans[i].internal_id = i + 1
            self.max_vlan_internal_id = len(self.vlans)

        self.changes = []
        self._emit('changelist_changed')
        # Always show a finished dialog. Otherwise, if you configuration
        # changes are made, the status window is gone so fast it feels
        # like the changes aren't really written (and if we have real
        # changes to commit, one extra second doesn't matter much).
        self._emit('status_changed', "Finished committing changes...")
        time.sleep(1)
        self._emit('status_changed', None)

    def commit_port_description_change(self, port, name):
        """
        Change the description of a port.
        """
        raise NotImplementedError()

    def commit_vlan_description_change(self, vlan, description):
        """
        Change the description of a vlan.
        """
        raise NotImplementedError()

    def commit_vlan_memberships(self, vlan, memberships):
        """
        Change the port memberships of a vlan. memberships is a list
        containing, for each port, in order, either Vlan.NOTMEMBER,
        Vlan.TAGGED or Vlan.UNTAGGED.
        """
        raise NotImplementedError()

    def commit_pvids(self, pvids):
        """
        Change the pvid settings of all ports. pvids is a list
        containing, for each port, in order, the vlan dotq_id for the PVID.
        """
        raise NotImplementedError()

    def commit_vlan_delete(self, vlan):
        """
        Delete the given vlan.
        """
        raise NotImplementedError()

    def get_status(self):
        """
        Retrieve current status of the switch.
        """
        raise NotImplementedError()


class Port(metaclass=MetaSignals):
    signals = ['details_changed']

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        emit_signal(self, name, self, *args)

    def __init__(self, switch, num, description, pvid, **kwargs):
        """
        Represents a vlan, consisting of an internal id (used to
        identify the vlan in the switch), the 802.11q id associated with
        the vlan and a name.
        """
        self.switch = switch
        self.num = num

        # TODO: Maybe editable attributes should be generalized?
        self._description = description
        self._pvid = pvid

        self.__dict__.update(kwargs)

        super(Port, self).__init__()

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, value):
        if value != self._description:
            self.switch.queue_change(PortDescriptionChange(self, value, self._description))
            self._description = value
            self._emit('details_changed')

    @property
    def pvid(self):
        return self._pvid

    @pvid.setter
    def pvid(self, value):
        if value != self._pvid:
            self.switch.queue_change(PortPVIDChange(self, value, self._pvid))
            self._pvid = value
            self._emit('details_changed')

    up = property(lambda self: self.link_status != 'Down')

    def __repr__(self):
        return u"Port %s: %s" % (self.num, self.description)


class Vlan(metaclass=MetaSignals):
    # Constants for the PortVLanMembershipChange. The values are also
    # the ones used by the FS726 HTTP interface.
    NOTMEMBER = 0
    TAGGED = 1
    UNTAGGED = 2

    signals = ['memberships_changed', 'details_changed']

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        emit_signal(self, name, self, *args)

    def __init__(self, switch, internal_id, dotq_id, name, **kwargs):
        """
        Represents a vlan, consisting of an internal id (used to
        identify the vlan in the switch), the 802.11q id associated with
        the vlan and a name.
        """
        self.switch = switch
        self.internal_id = internal_id
        self.dotq_id = dotq_id
        # Map a Port object to either NOTMEMBER, TAGGED or UNTAGGED
        self.ports = {}
        self._name = name

        self.__dict__.update(kwargs)

    def set_port_membership(self, port, membership):
        """
        Change the membership type of the given port. membership should
        be one of TAGGED, UNTAGGED or NOTMEMBER.
        """
        # TODO: Replace this function with some fancy wrapper around
        # self.ports
        old = self.ports[port]
        if old != membership:
            self.switch.queue_change(PortVlanMembershipChange((port, self), membership, old))
            self.ports[port] = membership
            self._emit('memberships_changed', port, membership)

            if membership == Vlan.UNTAGGED:
                # Also update the pvid for the port
                port.pvid = self.dotq_id

                # A port can only be untagged in one Vlan at a time, so
                # remove it from the previous one.
                for vlan in self.switch.vlans:
                    if vlan.ports[port] == Vlan.UNTAGGED and vlan != self:
                        vlan.set_port_membership(port, Vlan.NOTMEMBER)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if value != self._name:
            self.switch.queue_change(VlanNameChange(self, value, self._name))
            self._name = value
            self._emit('details_changed')

    def __repr__(self):
        return u"VLAN %s: %s (802.11q ID %s)" % (self.internal_id, self.name, self.dotq_id)


######################################################################
# Classes describing changes to the switch state
######################################################################

class Change(object):
    def __init__(self, what, how, old):
        """
        Creates a new change object, containing a what, a how and an old
        value.  Subclasses should define what these mean, this class
        only stores the values.
        """
        self.what = what
        self.how = how
        self.old = old


class VlanNameChange(Change):
    """
    Record the change of a vlan name. Constructor arguments:
    what: Vlan object
    how: new name (string)
    old: old name (string)
    """

    def merge_with(self, other):
        if (isinstance(other, VlanNameChange) and other.what == self.what):

            if (self.how == other.old):
                # This changes cancels the other change, remove them
                # both
                return (None, [])
            else:
                # This change replaces the other change. Note this actually
                # means this changes ends up in the position of the other
                # change in the changelist.
                self.old = other.old
                return (None, [self])

        # In all other cases, keep all of them
        return (self, [other])

    def __str__(self):
        return 'Changing vlan %d name to: %s' % (self.what.dotq_id, self.how)


class AddVlanChange(Change):
    """
    Record the addition of a vlan. Constructor arguments:
    what: Vlan object
    how: None
    old: None
    """
    def merge_with(self, other):
        if (isinstance(other, DeleteVlanChange) and other.what.dotq_id == self.what.dotq_id):
            # When re-adding a vlan wit the same dotq_id, we re-use
            # the existing vlan in the switch. Ideally, we would
            # just delete the vlan and then create a new one, but
            # we can't always find an ordering of operations that
            # satisfies all dependencies (especially when
            # considering PVIDs).
            self.what.internal_id = other.what.internal_id
            changes = []
            # Instead of deleting the vlan, we now have to record
            # all changes to turn it into a brand new vlan
            # separately.
            if self.what.name != other.what.name:
                changes.append(VlanNameChange(self.what, self.what.name, other.what.name))
            for port in self.what.ports:
                if self.what.ports[port] != other.what.ports[port]:
                    changes.append(PortVlanMembershipChange(
                        (port, self.what), self.what.ports[port], other.what.ports[port]))
            return (None, changes)

        return (self, [other])

    def __str__(self):
        return 'Adding vlan %d' % (self.what.dotq_id)


class DeleteVlanChange(Change):
    """
    Record the removal of a vlan. Constructor arguments:
    what: Vlan object
    how: None
    old: None
    """
    def merge_with(self, other):
        if (isinstance(other, VlanNameChange) and other.what == self.what):
            # No need to change the name of a removed vlan (but do
            # copy the old value, in case we are later merged with
            # an AddVlanChange)
            self.what.name = other.old
            return (self, [])
        elif (isinstance(other, PortVlanMembershipChange) and other.vlan == self.what):
            # No need to change memberships in a removed vlan (but
            # do copy the old value, in case we are later merged
            # with an AddVlanChange)
            self.what.ports[other.port] = other.old
            return (self, [])
        elif (isinstance(other, AddVlanChange) and other.what == self.what):
            # Removing a previously added vlan cancels both changes
            return (None, [])
        else:
            return (self, [other])

    def __str__(self):
        return 'Removing vlan %d' % (self.what.dotq_id)


class PortDescriptionChange(Change):
    """
    Record the change of a port description. Constructor arguments:
    what: Port object
    how: new description (string)
    old: old description (string)
    """

    def merge_with(self, other):
        if (isinstance(other, PortDescriptionChange) and other.what == self.what):
            if (self.how == other.old):
                # This changes cancels the other change, remove them
                # both
                return (None, [])
            else:
                # This change replaces the other change. Note this actually
                # means this changes ends up in the position of the other
                # change in the changelist.
                self.old = other.old
                return (None, [self])

        # In all other cases, keep all of them
        return (self, [other])

    def __str__(self):
        return 'Changing port %d description to: %s' % (self.what.num, self.how)


class PortPVIDChange(Change):
    """
    Record the change of a vlan name. Constructor arguments:
    what: Port object
    how: new vlan to use for the PVID
    old: old vlan to use for the PVID
    """

    def merge_with(self, other):
        if (isinstance(other, PortPVIDChange) and other.what == self.what):
            if (self.how == other.old):
                # This change cancels the other change, remove them
                # both
                return (None, [])
            else:
                # This change replaces the other change. Note this actually
                # means this changes ends up in the position of the other
                # change in the changelist.
                self.old = other.old
                return (None, [self])

        # In all other cases, keep all of them
        return (self, [other])

    def __str__(self):
        return 'Changing PVID for port %d to %d' % (self.what.num, self.how)


class PortVlanMembershipChange(Change):
    """
    Record the change of membership of a given Port in a given Vlan.
    what: (Port, Vlan) tuple
    how: Vlan.NOTMEMBER, Vlan.TAGGED, Vlan.UNTAGGED
    old: old value
    """

    port = property(lambda self: self.what[0])
    vlan = property(lambda self: self.what[1])

    def merge_with(self, other):
        if (isinstance(other, PortVlanMembershipChange) and other.what == self.what):

            if (self.how == other.old):
                # This changes cancels the other change, remove them
                # both
                return (None, [])
            else:
                # This change replaces the other change. Note this actually
                # means this changes ends up in the position of the other
                # change in the changelist.
                self.old = other.old
                return (None, [self])

        # In all other cases, keep both of them
        return (self, [other])

    def __str__(self):
        display = {Vlan.TAGGED: "tagged", Vlan.UNTAGGED: "untagged"}

        if self.old == Vlan.NOTMEMBER:
            return 'Adding port %d to vlan %d (%s)' % (self.port.num, self.vlan.dotq_id, display[self.how])
        elif self.how == Vlan.NOTMEMBER:
            return 'Removing port %d from vlan %d' % (self.port.num, self.vlan.dotq_id)
        else:
            return 'Changing port %d in vlan %d from %s to %s' % (
                self.port.num, self.vlan.dotq_id, display[self.old], display[self.how])
