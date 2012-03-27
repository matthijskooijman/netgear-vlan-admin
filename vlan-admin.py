#!/usr/bin/python

import urllib, urllib2
import re
import sys
import time
import os.path
import pickle
import configobj
import validate
import collections
from BeautifulSoup import BeautifulSoup
import urwid
from StringIO import StringIO

config_filename = os.path.expanduser("~/.config/vlan-admin.conf")

ui = None

# Some machinery to load a cached version of the settings, to speed up
# debugging.
write = False
load = False

logfile = None

def log(text):
    if logfile:
        logfile.write(text + "\n")
        logfile.flush()
    if ui:
        ui.log(text)
    else:
        # Shouldn't normally happen, but this can happen when debugging
        # with write == True
        print(text)

class CommitException(Exception):
    pass

class LoginException(Exception):
    pass

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
        if (isinstance(other, VlanNameChange) and
            other.what == self.what):

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

    def __unicode__(self):
        return 'Changing vlan %d name to: %s' % (self.what.dotq_id, self.how)

class AddVlanChange(Change):
    """
    Record the addition of a vlan. Constructor arguments:
    what: Vlan object
    how: None
    old: None
    """
    def merge_with(self, other):
        if (isinstance(other, DeleteVlanChange) and
            other.what.dotq_id == self.what.dotq_id):
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
                        changes.append(PortVlanMembershipChange((port, self.what), self.what.ports[port], other.what.ports[port]))
                return (None, changes)

        return (self, [other])

    def __unicode__(self):
        return 'Adding vlan %d' % (self.what.dotq_id)

class DeleteVlanChange(Change):
    """
    Record the removal of a vlan. Constructor arguments:
    what: Vlan object
    how: None
    old: None
    """
    def merge_with(self, other):
        if (isinstance(other, VlanNameChange) and
            other.what == self.what):
                # No need to change the name of a removed vlan (but do
                # copy the old value, in case we are later merged with
                # an AddVlanChange)
                self.what.name = other.old
                return (self, [])
        elif (isinstance(other, PortVlanMembershipChange) and
              other.vlan == self.what):
                # No need to change memberships in a removed vlan (but
                # do copy the old value, in case we are later merged
                # with an AddVlanChange)
                self.what.ports[other.port] = other.old
                return (self, [])
        elif (isinstance(other, AddVlanChange) and
              other.what == self.what):
                # Removing a previously added vlan cancels both changes
                return (None, [])
        else:
                return (self, [other])

    def __unicode__(self):
        return 'Removing vlan %d' % (self.what.dotq_id)

class PortDescriptionChange(Change):
    """
    Record the change of a port description. Constructor arguments:
    what: Port object
    how: new description (string)
    old: old description (string)
    """

    def merge_with(self, other):
        if (isinstance(other, PortDescriptionChange) and
            other.what == self.what):

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

    def __unicode__(self):
        return 'Changing port %d description to: %s' % (self.what.num, self.how)

class PortPVIDChange(Change):
    """
    Record the change of a vlan name. Constructor arguments:
    what: Port object
    how: new vlan to use for the PVID
    old: old vlan to use for the PVID
    """

    def merge_with(self, other):
        if (isinstance(other, PortPVIDChange) and
            other.what == self.what):

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

    def __unicode__(self):
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
        if (isinstance(other, PortVlanMembershipChange) and
            other.what == self.what):

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

    def __unicode__(self):
        display = {Vlan.TAGGED: "tagged", Vlan.UNTAGGED: "untagged"}

        if self.old == Vlan.NOTMEMBER:
            return 'Adding port %d to vlan %d (%s)' % (self.port.num, self.vlan.dotq_id, display[self.how])
        elif self.how == Vlan.NOTMEMBER:
            return 'Removing port %d from vlan %d' % (self.port.num, self.vlan.dotq_id)
        else:
            return 'Changing port %d in vlan %d from %s to %s' % (self.port.num, self.vlan.dotq_id, display[self.old], display[self.how])

class Vlan(object):
    # Constants for the PortVLanMembershipChange. The values are also
    # the ones used by the FS726 HTTP interface.
    NOTMEMBER = 0
    TAGGED = 1
    UNTAGGED = 2

    __metaclass__ = urwid.MetaSignals
    signals = ['memberships_changed', 'details_changed']

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        urwid.emit_signal(self, name, self, *args)

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

class Port(object):
    __metaclass__ = urwid.MetaSignals
    signals = ['details_changed']

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        urwid.emit_signal(self, name, self, *args)

    def __init__(self):
        pass

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

        self._pvid = None # Should be set afterwards

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

    def __repr__(self):
        return u"Port %s: %s (speed: %s, speed setting: %s, flow control: %s, link status = %s)" % (self.num, self.description, self.speed, self.speed_setting, self.flow_control, self.link_status)

class FS726T(object):
    # Autoregister signals
    __metaclass__ = urwid.MetaSignals
    signals = ['changelist_changed', 'details_changed', 'portlist_changed', 'vlanlist_changed', 'status_changed']

    def __init__(self, address = None, password = None, config = None):
        self.address = address
        self.password = password
        self.ports = []
        self.vlans = []
        self.dotq_vlans = {}
        self.config = config
        self.changes = []
        self.max_vlan_internal_id = 0

        self.product = None
        self.firmware_version = None
        self.protocol_version = None
        self.ip_config = None
        self.ip_config = None
        self.ip_address = None
        self.ip_netmask = None
        self.ip_gateway = None
        self.mac_address = None
        self.hostname = None
        self.location = None
        self.login_timeout = None
        self.uptime = None

        super(FS726T, self).__init__()

    def _emit(self, name, *args):
        """
        Convenience function to emit signals with self as first
        argument.
        """
        urwid.emit_signal(self, name, self, *args)

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

    def request(self, path, data = None, status = None):
        if status:
            self._emit('status_changed', status)

        url = "http://%s%s" % (self.address, path)

        if data and not isinstance(data, basestring):
            data = urllib.urlencode(data)

        if data != None:
            log("HTTP POST request to %s (POST data %s)" % (url, str(data)))
        else:
            log("HTTP GET request to %s" % url)

        response = urllib2.urlopen(url, data).read()
        log('Done')

        if "<input type=submit value=' Login '>" in response:
            self.do_login()
            return self.request(path, data, status)
        if "Only one user can login" in response:
            raise LoginException("Can only login from a single IP address: Log out the other client first")
        return response

    def do_login(self):
        """
        Log into this device. Be sure to always call do_logout as well,
        to prevent other users from getting locked out.
        """

        # Apparently, login is done on an IP basis, not tracked by cookies.
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('passwd', self.password),
            ('post_url', "/cgi/device"),
        ]
        html = self.request("/cgi/device", data, "Logging in...")

        # Succesful login
        if '<h1>Switch Status</h1>' in html:
            return

        # See if we can find an error message
        error = re.search("<font color=#336699 size=3><br><b>(.*)<br><br><input type=submit value='Continue'><br><br>", html)
        if (error):
            raise LoginException("Error occured at login. Device said: %s" % error.group(1))

        # Other error?
        raise LoginException("Login seems to have failed, but no error message could be found")

    def do_logout(self):
        """
        Log out. This is important, since the device does not allow parallel
        sessions, so not logging out means you'll have to wait for the
        session timeout before you can log in again.
        """
        try:
            self.request("/cgi/logout", status = "Logging out...")
        except urllib2.HTTPError,e:
            if e.code == 404:
                sys.stderr.write("Ignoring logout error, we're probably not logged in.\n")
            else:
                raise


    def commit_all(self):
        def changes_of_type(type):
            return [c for c in self.changes if isinstance(c, type)]

        if not self.changes:
            raise CommitException("No changes to commit")

        self._emit('status_changed', "Committing changes...")

        write_config = False

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
                self.config['vlan_names']['vlan%d' % change.what.dotq_id] = change.how
                write_config = True
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
                # Remove the name from the config
                self.config['vlan_names'].pop('vlan%d' % change.what.dotq_id, None)
                write_config = True
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
                if not port in changes:
                        # Just use the old (unmodified) value
                        changelist.append(vlan.ports[port])
                else:
                    (new, old) = changes[port]

                    if not port in ports:
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
        for vlan, ports in first_pass.iteritems():
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
        if delete_vlans:
            for i in range(0, len(self.vlans)):
                self.vlans[i].internal_id = i + 1
            self.max_vlan_internal_id = len(self.vlans)

        if write_config:
            self.config.write()

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
        Change the name of a port.
        """
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('portset', port.num - 1), # "portset" numbers from 0
            ('port_des', name),
            ('post_url', '/cgi/portdetail'),
        ]

        self.request("/cgi/portdetail=%s" % (port.num - 1), data, "Committing port %d description..." % (port.num))

    def commit_vlan_memberships(self, vlan, memberships):
        """
        Change the port memberships of a vlan. memberships is a list
        containing, for each port, in order, either Vlan.NOTMEMBER,
        Vlan.TAGGED or Vlan.UNTAGGED.

        If the vlan is new (no internal_id yet), an internal id is
        assigned (updating max_vlan_internal_id) and the vlan is
        created.
        """
        status = "Committing vlan %d memberships..." % (vlan.dotq_id)
        # If no internal id is present yet, assign the next one. Calling
        # setvid with a non-existing tag_id will make the switch
        # conveniently create the vlan.
        if vlan.internal_id == None:
            self.max_vlan_internal_id += 1
            vlan.internal_id = self.max_vlan_internal_id
            status = "Creating vlan %d..." % (vlan.dotq_id)

        # Do not change the order of parameters, that breaks the request :-S
        # Note that in the webgui, the 'vid' parameter is only present
        # in the add new vlan request, but doesn't seem to break the
        # update vlan request either, so we just add it unconditionally.
        data = [
            ('tag_id', vlan.internal_id),
            ('vid', vlan.dotq_id),
            ('post_url', '/cgi/setvid'),
            ('vid_mem', ','.join([str(m) for m in memberships])),
        ]

        self.request("/cgi/setvid=%s" % (vlan.internal_id), data, status)

    def commit_pvids(self, pvids):
        """
        Change the pvid settings of all ports. vlans is a list
        containing, for each port, in order, the vlan dotq_id for the PVID.
        """
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('tag_id', 255),
        ] + [
            ('dvid', pvid) for pvid in pvids
        ] + [
            ('post_url', '/cgi/pvid'),
        ]

        self.request("/cgi/pvid", data, "Committing PVID settings..")

    def commit_vlan_delete(self, vlan):
        """
        Delete the given vlan. Does not renumber the remaining vlans or
        update the max_vlan_internal_id value.
        """
        # Do not change the order of parameters, that breaks the request :-S
        data = [
            ('tag_id', vlan.internal_id),
            ('del_tag', 'on'),
            ('post_url', '/cgi/delvid'),
            ('vid_mem', ''),
        ]
        self.request("/cgi/setvid=%s" % (vlan.internal_id), data, "Deleting vlan %d..." % (vlan.dotq_id))

    def get_status(self):
        soup = BeautifulSoup(self.request("/cgi/device", status = "Retrieving switch status..."), convertEntities=BeautifulSoup.HTML_ENTITIES)

        try:
            self.parse_status(soup)
            self._emit('details_changed')
            self._emit('portlist_changed')
        except AttributeError as e:
            # Print HTML for debugging
            print soup
            raise

        self._emit('status_changed', None)

    def parse_status(self, soup):
        #####################################
        # Parse switch information
        #####################################
        h1 = soup.find(text="Switch Status")
        h1_table = h1.findParent('table')
        table = h1_table.findNext("table")

        rows = table.findAll('tr')

        for row in rows:
            tds = row.findAll('td')
            key = remove_html_tags(tds[0].text)
            value = remove_html_tags(tds[1].text)
            if key == 'Product Name':
                self.product = value
            elif key == 'Firmware Version':
                self.firmware_version = value
            elif key == 'Protocol Version':
                self.protocol_version = value
            elif key == 'DHCP' and value == 'Disable':
                self.ip_config = 'Static'
            elif key == 'DHCP' and value == 'Enable':
                self.ip_config = 'DHCP'
            elif key == 'IP address':
                self.ip_address = value
            elif key == 'Subnet mask':
                self.ip_netmask = value
            elif key == 'Default gateway':
                self.ip_gateway = value
            elif key == 'MAC address':
                self.mac_address = value
            elif key == 'System Name':
                self.hostname = value
            elif key == 'Location Name':
                self.location = value
            elif key == 'Login Timeout (minutes)':
                self.login_timeout = value + ' minutes'
            elif key == 'System UpTime':
                self.uptime = value
            else:
                sys.stderr.write('Ignoring unknown table row: %s = %s\n' % (key, value))

        #####################################
        # Parse port information
        #####################################
        h1 = soup.find(text="PORT Status").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

        # The top two rows are the header (but nobody bothered putting them
        # in a thead, of course). Note that the second header row is
        # empty...
        port_rows = rows[1:]

        speed = "unknown"
        self.ports = []
        for row in port_rows:
            # There are rows containing a single th tag that specify the
            # speed for the subsequent ports.
            ths = row.findAll('th')
            if (ths and len(ths) == 1):
                speed = ths[0].text
                continue

            tds = row.findAll('td')
            if (tds):
                # Each row contains info for two ports, so iterate them
                for port_tds in (tds[0:5], tds[5:10]):
                    (num, speed_setting, flow_control, link_status, description) = [td.text for td in port_tds]
                    assert len(self.ports) == int(num) - 1, "Switch ports are not numbers consecutively?"

                    port = Port(self, int(num), speed, speed_setting, flow_control, link_status, description)
                    self.ports.append(port)

        #####################################
        # Parse vlan information
        #####################################
        h1 = soup.find(text="IEEE 802.1Q VLAN Settings").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

        # The second row contains a header td for every port (but no leading
        # td like the rows below, since the first row starts with a
        # rowspan="2" td).
        port_count = len(rows[1].findAll('td'))

        # The top two rows are the header (but nobody bothered putting them
        # in a thead, of course)
        vlan_rows = rows[2:]

        self.vlans = []
        self.dotq_vlans = {}
        for row in vlan_rows:
            tds = row.findAll('td')
            # We assume the vlans are listed in order of their internal
            # id (e.g., order of creation, one-based)
            internal_id = len(self.vlans) + 1
            # The first td shows the 802.11q id
            dotq_id = int(tds[0].text)

            # Create the vlan descriptor
            name = self.config['vlan_names'].get('vlan%d' % dotq_id, '')
            vlan = Vlan(self, internal_id, dotq_id, name)
            self.vlans.append(vlan)
            self.dotq_vlans[dotq_id] = vlan

            assert len(tds) == len(self.ports) + 1, "VLAN table has wrong number of ports?"
            for port in self.ports:
                # We skip td[0] (which contains a header), since we use
                # the 1-based port number
                td = tds[port.num]
                if td.text == '':
                    vlan.ports[port] = Vlan.NOTMEMBER
                elif td.text == 'T':
                    vlan.ports[port] = Vlan.TAGGED
                elif td.text == 'U':
                    vlan.ports[port] = Vlan.UNTAGGED
                else:
                    sys.stderr.write('Ignoring unknown vlan/port status: %s \n' % td.vlaue)

        self.max_vlan_internal_id = len(self.vlans)

        #####################################
        # Parse PVID information
        #####################################
        h1 = soup.find(text="IEEE 802.1Q PVID Table").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

        # The top row is the header (but nobody bothered putting it
        # in a thead, of course).
        pvid_rows = rows[1:]

        for row in pvid_rows:
            tds = row.findAll('td')
            if (tds):
                # Each row contains info for four ports, so iterate them
                for port_tds in (tds[0:2], tds[2:4], tds[4:6], tds[6:8]):
                    (num, pvid) = [td.text for td in port_tds]
                    if num:
                        # We set the internal value here, to prevent a
                        # change being generated in the changelist.
                        self.ports[int(num) - 1]._pvid = int(pvid)


class PortVlanMatrix(urwid.WidgetWrap):
    """
    Widget that displays a matrix of ports versus vlans and allows to
    edit the vlan memberships.
    """
    def __init__(self, interface, switch, focus_change, vlan_keypress_handler):
        self.interface = interface
        self.switch = switch
        self.focus_change = focus_change
        self.vlan_keypress_handler = vlan_keypress_handler

        super(PortVlanMatrix, self).__init__(None)

        self.create_widgets(switch)

        # When the switch (re)loads the portlist, just recreate the widgets
        urwid.connect_signal(switch, 'portlist_changed', self.create_widgets)
        urwid.connect_signal(switch, 'vlanlist_changed', self.create_widgets)

    def create_widgets(self, switch):
        # We build a matrix using a Pile of Columns. This allows us to
        # properly navigate through the matrix. We can't inverse this
        # (using a Columns of Piles), since there is no code to
        # preserver the vertical "preferred cursor position", only
        # for horizontal.

        # Find out the maximum vlan name length, so we can make all the
        # vlan names in the first column have the same width. Ensure
        # it's always 20 characters wide.
        self.vlan_header_width = max([20] + [len(v.name) for v in self.switch.vlans]) + 10

        rows = []

        # Create the header row, containing port numbers
        row = [('fixed', self.vlan_header_width, urwid.Text(""))]
        for port in switch.ports:
            row.append(
                ('fixed', 4, urwid.Text(" %02d " % port.num))
            )
        rows.append(urwid.Columns(row))

        # Create a row for each vlan
        for vlan in switch.vlans:
            widget = urwid.Text("")
            def update_vlan_header(vlan_header, vlan):
                vlan_header.set_text("%4s: %s" % (vlan.dotq_id, vlan.name))
            update_vlan_header(widget, vlan)
            urwid.connect_signal(vlan, 'details_changed', update_vlan_header, weak_args=[widget])

            widget = KeypressAdapter(widget, self.vlan_keypress_handler)
            # For the focus_change handler
            widget.base_widget.vlan = vlan
            urwid.connect_signal(widget.base_widget, 'focus', self.focus_change)

            widget = urwid.AttrMap(widget, None, 'focus')
            row = [('fixed', self.vlan_header_width, widget)]

            for port in switch.ports:
                widget = PortVlanWidget(self.interface, port, vlan)
                urwid.connect_signal(widget, 'focus', self.focus_change)
                row.append(
                    ('fixed', 4, widget)
                )
            rows.append(urwid.Columns(row))

        self._w = urwid.Pile(rows)


    def keypress(self, size, key):
        def add_vlan(input):
            try:
                dotq_id = int(input)
            except ValueError:
                self.interface.show_popup("Invalid VLAN id: '%s' (not a valid number)" % input)
                return

            if dotq_id < 1 or dotq_id > 4094:
                self.interface.show_popup("Invalid VLAN id: '%d' (valid values range from 1 up to and including 4094)" % dotq_id)
                return

            if dotq_id in self.switch.dotq_vlans:
                self.interface.show_popup("VLAN with id '%d' already exists" % dotq_id)
                return

            self.switch.add_vlan(dotq_id)

        if key == 'insert':
            self.interface.input_popup("802.1q VLAN ID?", add_vlan)
        else:
            return super(PortVlanMatrix, self).keypress(size, key)

        return None

    # We have a fixed size
    def sizing(self):
        return set([urwid.FIXED])

    def pack(self, size, focus=False):
        # Calculate our fixed size
        columns = self.vlan_header_width + len(self.switch.ports) * 4
        # Delegate the rows calculation to the Pile
        rows = self.rows((columns,))
        return (columns, rows)



class PortVlanWidget(urwid.FlowWidget):
    """
    Class to display and edit a port / vlan combination.
    """

    def __init__(self, interface, port, vlan):
        super(PortVlanWidget, self).__init__()
        self._selectable = True
        self.interface = interface
        self.port = port
        self.vlan = vlan

        def memberships_changed(vlan, port, membership):
            if port is self.port:
                self._invalidate()

        # TODO: This signal handler prevents PortVlanWidget from being
        # cleaned up as long as the vlan is still around (even when it
        # is replaced by a new PortVlanWidget and not actually displayed
        # anymore). This means membership_changed should probably use a
        # weakref to self. We can't pass a weakref (proxy) to
        # memberships_changed to connect_signal, since then the function
        # object will be garbage collected directly. This needs probably
        # needs some support in urwid, but I haven't quite figured out
        # how exactly...
        urwid.connect_signal(vlan, 'memberships_changed', memberships_changed)

    def keypress(self, size, key):
        if key == 't' or key == 'T':
            self.vlan.set_port_membership(self.port, Vlan.TAGGED)
        elif key == 'U' or key == 'u':
            self.vlan.set_port_membership(self.port, Vlan.UNTAGGED)
        elif key == ' ' or key == 'backspace' or key == 'delete':
            if self.port.pvid == self.vlan.dotq_id:
                # We can't just remove an untagged membership, we need
                # to know where to point the PVID to.
                msg = "Cannot remove membership, due to PVID setting.\n"
                msg += "\nAssign this port into another vlan untagged to change the PVID."
                self.interface.show_popup(msg)
            else:
                self.vlan.set_port_membership(self.port, Vlan.NOTMEMBER)
        else:
            return key

        return None

    def render(self, size, focus=False):
        cols, = size
        text = " " * ((cols - 2) / 2)
        member = self.vlan.ports[self.port]
        if member == Vlan.TAGGED:
            text += "TT"
            attr = "tagged"
        elif member == Vlan.UNTAGGED:
            text += "UU"
            attr = "untagged"
        else:
            assert member == Vlan.NOTMEMBER
            attr = "none"
            text += "  "
        if focus:
            attr += "_focus"

        text += " " * (cols - 2 - ((cols - 2) / 2))

        return urwid.TextCanvas([text], [[(attr, len(text))]])

    def rows(self, size, focus=False):
        return 1

class PortWidget(urwid.Text):
    """
    Class to display and edit a port.
    """

    def __init__(self, port):
        super(PortWidget, self).__init__(None)
        self._selectable = True
        self.port = port

    def set_text(self, text):
        # ignored
        if text != None:
            raise NotImplementedError()

    def get_text(self):
        text = "%02d: %s" % (self.port.num, self.port.description)
        attrib = []
        return text, attrib

    def keypress(self, size, key):
        return key

class TopLine(urwid.LineBox):
    """
    A box like LineBox, but containing just the top line
    """
    def __init__(self, original_widget, title = ''):
        super(TopLine, self).__init__(original_widget, title,
            tlcorner = u' ', trcorner = u' ', blcorner = u' ', 
            brcorner = u' ', rline = u' ', lline = u' ', bline = u' ')

class KeypressAdapter(urwid.WidgetPlaceholder):
    """
    This widget wraps another widget, but inserts a custom keypress
    handler before the keypress handler of the original widget.

    The keypress handler passed should be a function accepting a widget,
    size and a key argument (similar to the keypress method on Widgets).
    The widget argument contains this KeypressAdapter widget (use the
    original_widge / base_widget to get at the decorated widget).
    """
    def __init__(self, original_widget, keypress_handler):
        super(KeypressAdapter, self).__init__(original_widget)
        self.keypress_handler = keypress_handler

    def keypress(self, size, key):
        key = self.keypress_handler(self, size, key)
        if key and self.original_widget.selectable():
            key = self.original_widget.keypress(size, key)
        return key

    def selectable(self):
        # Make sure we get keypresses
        return True

# Support vim key bindings in the default widgets
urwid.command_map['j'] = 'cursor down'
urwid.command_map['k'] = 'cursor up'
urwid.command_map['h'] = 'cursor left'
urwid.command_map['l'] = 'cursor right'

class Interface(object):
    focus_text = 'black'
    normal_text = 'light gray'
    untagged_text = 'light blue'
    tagged_text = 'dark red'
    normal_bg = 'black'
    focus_bg = 'light gray'
    palette = [
        ('header', 'black', 'light gray'),
        ('none', normal_text, normal_bg),
        ('focus', focus_text, focus_bg),
        ('tagged', tagged_text, normal_bg),
        ('untagged', untagged_text, normal_bg),
        ('none_focus', normal_text, focus_bg),
        ('tagged_focus', tagged_text, focus_bg),
        ('untagged_focus', untagged_text, focus_bg),
        ('overlay', 'white', 'dark blue'),
    ]

    # (Label, attribute, editable?)
    port_attrs = [[
        ('Port number', 'num', False),
        ('Description', 'description', True),
        ('Port speed', 'speed', False),
        ('Configured speed', 'speed_setting', False),
        ('Flow control', 'flow_control', False),
        ('Link', 'link_status', False),
        ('PVID', 'pvid', False),
    ]]

    vlan_attrs = [[
        ('802.11q VLAN number', 'dotq_id', False),
        ('VLAN name', 'name', True),
    ]]

    switch_attrs = [
        [
            ('Product', 'product', False),
            ('Firmware version', 'firmware_version', False),
            ('Protocol version', 'protocol_version', False),
            ('MAC address', 'mac_address', False),
        ], [
            ('IP configuration', 'ip_config', False),
            ('IP address', 'ip_address', False),
            ('IP netmask', 'ip_netmask', False),
            ('IP gateway', 'ip_gateway', False),
        ], [
            ('Hostname', 'hostname', False),
            ('Location', 'location', False),
            ('Login timeout', 'login_timeout', False),
            ('Uptime', 'uptime', False),
        ]
    ]


    def __init__(self, switch):
        self.switch = switch
        self._overlay_widget = None
        super(Interface, self).__init__()

    def start(self):
        self.create_widgets()

        urwid.connect_signal(self.switch, 'status_changed', self.status_changed)

        self.loop = urwid.MainLoop(self.main_widget, palette=Interface.palette, unhandled_input=self.unhandled_input)
        self.loop.screen.run_wrapper(self.run)

    def create_widgets(self):
        self.header = header = urwid.Text("Connected to %s" % self.switch.address, align='center')
        header = urwid.AttrMap(header, 'header')

        self.port_widgets = {}
        self.vlan_widgets = {}
        self.switch_widgets = {}
        port_details = self.create_details(Interface.port_attrs, self.port_widgets)
        vlan_details = self.create_details(Interface.vlan_attrs, self.vlan_widgets)

        switch_details = self.create_details(Interface.switch_attrs, self.switch_widgets, True)

        def fill_switch_details(switch):
            self.fill_details(Interface.switch_attrs, self.switch_widgets, switch)

        fill_switch_details(self.switch)

        switch_details = TopLine(switch_details, title="Connected switch")

        bottom = urwid.Columns([
            port_details,
            vlan_details,
        ])
        bottom = TopLine(bottom, 'Details')

        self.debug = urwid.Text('')
        dbg = TopLine(self.debug, 'Debug')

        self.changelist = urwid.Text('')
        changelist = TopLine(self.changelist, 'Unsaved changes')
        urwid.connect_signal(self.switch, 'changelist_changed', self.fill_changelist)
        self.fill_changelist(self.switch)

        def matrix_focus_change(widget):
            if hasattr(widget, 'port'):
                self.fill_details(Interface.port_attrs, self.port_widgets, widget.port)
            if hasattr(widget, 'vlan'):
                self.fill_details(Interface.vlan_attrs, self.vlan_widgets, widget.vlan)

        def vlan_keypress_handler(widget, size, key):
            if key == 'delete':
                self.try_delete_vlan(widget.base_widget.vlan)
            else:
                return key
            return None

        matrix = urwid.Padding(PortVlanMatrix(self, self.switch, matrix_focus_change, vlan_keypress_handler), align='center')
        matrix = TopLine(matrix, 'VLAN / Port mappings')

        pile = urwid.Pile([('flow', switch_details),
                           ('flow', matrix),
                           ('flow', bottom),
                           ('flow', changelist),
                           ('flow', dbg),
                          ])

        def main_keypress_handler(widget, size, key):
            if key == 'tab':
                if pile.get_focus() is matrix:
                    pile.set_focus(bottom)
                    bottom.base_widget.set_focus(port_details)
                elif bottom.base_widget.get_focus() is port_details:
                    bottom.base_widget.set_focus(vlan_details)
                else:
                    pile.set_focus(matrix)
            else:
                return key
            return None

        body = KeypressAdapter(pile, main_keypress_handler)
        self.main_widget = urwid.Filler(body, valign = 'top')

    @property
    def overlay_widget(self):
        return self._overlay_widget

    @overlay_widget.setter
    def overlay_widget(self, widget):
        if widget:
            self._overlay_widget = widget
            widget = urwid.Padding(widget, left=1, right=1, width=('relative', 100))
            widget = urwid.LineBox(widget)
            widget = urwid.AttrMap(widget, 'overlay')
            overlay= urwid.Overlay(
                top_w = widget,
                bottom_w = self.main_widget,
                align = 'center',
                width = 75,
                valign = ('fixed top', 4),
                height = 10,
            )
            self.loop.widget = overlay
        else:
            self.loop.widget = self.main_widget

        # Force a screen redraw (in case we're called from a keypress
        # handler which takes a while to copmlete, for example).
        self.loop.draw_screen()

    def run(self):
        self.loop.draw_screen()

        # Get switch status
        if not load:
            self.switch.get_status()
        else:
            self.status_changed(None, None)

        log("Starting mainloop")
        self.loop.run()

    def create_details(self, attrs, widget_dict, gridflow = False):
        """
        Create a widget showing attributes of an object as specified in
        attrs. Is initially empty, call fill_details to fill in info.

        widget_dict should be an empty dict, which this method will
        fill. The same dict should be passed to fill_details.

        Returns the Widget created.
        """
        max_label_width = max([len(l) for c in attrs for (l, a, e) in c ])

        top_columns = []
        for column in attrs:
            widgets = []
            for (label_text, attr, edit) in column:
                label = urwid.Text(label_text + ":")
                if edit:
                    widget = urwid.Edit()
                    # fill_details will store the object displayed in here
                    widget.obj = None
                    def detail_changed(widget, text, attr):
                        if widget.obj:
                            setattr(widget.obj, attr, text)

                    # That attr argument will be passed to the signal
                    # handler
                    urwid.connect_signal(widget, 'change', detail_changed, attr)
                else:
                    widget = urwid.Text('')

                columns = urwid.Columns([
                    ('fixed', max_label_width + 4, label),
                    widget,
                ])

                if gridflow:
                    widgets.append(columns)
                else:
                    widgets.append(('flow', columns))
                widget_dict[attr] = widget
            top_columns.append(urwid.Pile(widgets))

        if len(top_columns) == 1:
            return top_columns[0]
        else:
            return urwid.Columns(top_columns)

    def fill_details(self, attrs, widget_dict, obj):
        """
        Fills the attribute widgets created by create_details.

        widget_dict and attrs must be the same dict and list as passed
        to create_details. obj is the object to retrieve attributes
        from.
        """
        for column in attrs:
            for (label_text, attr, edit) in column:
                text = str(getattr(obj, attr))
                w = widget_dict[attr]
                if edit:
                    w.obj = obj
                    w.set_edit_text(text)
                else:
                    w.set_text(text)
        # We abuse the widget_dict a bit to store the currently visible
        # object, so we can unregister any signals
        prev = widget_dict.get('active_object', None)
        if prev != obj:
            if prev:
                prev_handler = widget_dict['active_object_handler']
                urwid.disconnect_signal(prev, 'details_changed', prev_handler)

            # Store the active opbject, so we can disconnect the signal
            # later
            widget_dict['active_object'] = obj

            # Add a new signal handler, so the details get updated when they
            # change.
            def update_details(obj):
                self.fill_details(attrs, widget_dict, obj)
            urwid.connect_signal(obj, 'details_changed', update_details)

            # Store the signal handler, since we need it to be identical
            # when disconnecting the signal later on.
            widget_dict['active_object_handler'] = update_details

    def fill_changelist(self, switch):
        if switch.changes:
            text = u'\n'.join([unicode(c) for c in switch.changes])
        else:
            text = 'No changes'
        self.changelist.set_text(text)

    def unhandled_input(self, key):
        if key == 'q' or key == 'Q' or key == 'f10':
            raise urwid.ExitMainLoop()
        elif key == 'f11':
            try:
                self.switch.commit_all()
            except CommitException as e:
                self.show_popup(unicode(e))
        else:
            log("Unhandled keypress: %s" % str(key))

        return False

    def status_changed(self, obj, new_status):
        """
        Show the current status on the screen. Pass a new status of None
        to remove the status popup and allow the interface to be used again.

        Intended for use as a signal handler, leave the "obj" parameter
        to None if call this function directly.
        """
        if new_status:
            text = urwid.Text(new_status, align='center')
            self.overlay_widget = urwid.Filler(text)
        else:
            self.overlay_widget = None

    def show_popup(self, text):
        # Create a SelectableText overlay that hides the overlay on any
        # keypress
        def hide_on_keypress(widget, size, key):
            self.overlay_widget = None
            return None

        widget = urwid.Text(text + "\n\n\nPress any key...", align='left')
        widget = KeypressAdapter(widget, hide_on_keypress)
        self.overlay_widget = urwid.Filler(widget)

    def yesno_popup(self, text, yes_callback, no_callback = None):
        """
        Show a popup that allows to confirm/decline using y/n.
        """
        def handle_keypress(widget, size, key):
            if key == 'y' or key == 'Y':
                self.overlay_widget = None
                yes_callback()
            elif key == 'n' or key == 'N':
                self.overlay_widget = None
                if no_callback:
                    no_callback()
            else:
                return key
            return None

        text = urwid.Text(text, align='center')
        text = KeypressAdapter(text, handle_keypress)
        help = urwid.Text("Press 'y' to confirm, 'n' to decline")

        body = urwid.Filler(text, valign='top')
        self.overlay_widget = urwid.Frame(body, footer=help)


    def input_popup(self, text, callback, cancel = None):
        """
        Show a popup that allows to enter text. When enter is pressed,
        the given callback is called with the entered text. When f10 is
        pressed, the prompt is canceled and the (optional) cancel
        function is called with the text entered so far.
        """
        def handle_keypress(widget, size, key):
            if key == 'enter':
                self.overlay_widget = None
                callback(widget.base_widget.text)
            elif key == 'f10' or key == 'ctrl g':
                self.overlay_widget = None
                if cancel:
                    cancel(widget.base_widget.text)
            else:
                return key
            return None

        text = urwid.Text(text)
        edit = urwid.Edit()
        edit = KeypressAdapter(edit, handle_keypress)
        help = urwid.Text("Press enter to confirm, f10 or ^G to cancel")

        body = urwid.Filler(edit, valign='top')
        self.overlay_widget = urwid.Frame(body, header=text, footer=help)
    def try_delete_vlan(self, vlan):
        ports = [str(p.num) for p in self.switch.ports if p.pvid == vlan.dotq_id]
        if ports:
            msg = "Cannot remove vlan, some PVIDs still point to it (%s %s).\n"
            msg += "\nAssign these ports into another vlan untagged to change this."
            self.show_popup(msg % ('ports' if len(ports) > 1 else 'port', ', '.join(ports)))
        else:
            name = vlan.name if vlan.name else 'unnamed'
            self.yesno_popup("Are you sure you wish to delete VLAN %d (%s)?" % (vlan.dotq_id, name),
                             lambda: self.switch.delete_vlan(vlan))

    def log(self, text):
        # Note: This discards any existing markup
        self.debug.set_text(self.debug.text + text + "\n")
        # Force a screen redraw (in case we're called from a keypress
        # handler which takes a while to copmlete, for example).
        self.loop.draw_screen()

def remove_html_tags(data):
    p = re.compile(r'<.*?>')
    return p.sub('', data)


# This is the structure of the config file. We apply validation to
# make sure all sections are created, even when the config file starts
# out empty.
configspec = """
[vlan_names]
__many__ = string()
"""

def main():
    global ui, logfile

    logfile = open('vlan-admin.log', 'a')

    # Create the switch object
    config = configobj.ConfigObj(infile = config_filename, configspec = StringIO(configspec), create_empty = True, encoding='UTF8')
    config.validate(validate.Validator())

    if load:
        # Load the switch object from a debug file
        f = open('switch.dump', 'r')
        switch = pickle.load(f)
        switch.config.reload()
    else:
        # Create a new switch object
        switch = FS726T('192.168.1.253', 'password', config)

        if write:
            # Get the status now, since it seems the even handlers interfere
            # with the pickling.
            switch.get_status()

            # Dump the switch object
            f = open('switch.dump', 'w')
            pickle.dump(switch, f)

    # Create an interface for the switch
    ui = Interface(switch)
    ui.start()

    # When quitting, write out the configuration
    switch.config.write()

    logfile.close()

if __name__ == '__main__':
    main()
