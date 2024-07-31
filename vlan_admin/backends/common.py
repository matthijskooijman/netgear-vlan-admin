from urwid import MetaSignals, emit_signal


class Port(metaclass=MetaSignals):
    signals = ['details_changed']

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        emit_signal(self, name, self, *args)

    def __init__(self, switch, num, speed, speed_setting, flow_control, link_status, description):
        """
        Represents a vlan, consisting of an internal id (used to
        identify the vlan in the switch), the 802.11q id associated with
        the vlan and a name.
        """
        self.switch = switch
        self.num = num
        self.speed = speed
        self.speed_setting = speed_setting
        self.flow_control = flow_control
        self.link_status = link_status
        self._description = description

        self._pvid = None  # Should be set afterwards

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
        return u"Port %s: %s (speed: %s, speed setting: %s, flow control: %s, link status = %s)" % (
            self.num, self.description, self.speed, self.speed_setting, self.flow_control, self.link_status)


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

    def __init__(self, switch, internal_id, dotq_id, name):
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
