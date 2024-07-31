import bs4
import collections
import urllib.error
import urllib.parse
import urllib.request
import re
import sys
import time
from urwid import MetaSignals, emit_signal

from ..log import log
from .common import Port, Vlan
from .common import (
    AddVlanChange, DeleteVlanChange, PortDescriptionChange, PortPVIDChange, PortVlanMembershipChange, VlanNameChange
)


class CommitException(Exception):
    pass


class LoginException(Exception):
    pass


class FS726T(metaclass=MetaSignals):
    signals = ['changelist_changed', 'details_changed', 'portlist_changed', 'vlanlist_changed', 'status_changed']

    def __init__(self, address=None, password=None, config=None):
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

    def request(self, path, data=None, status=None, auto_login=True):
        if status:
            self._emit('status_changed', status)

        url = "http://%s%s" % (self.address, path)

        if data and not isinstance(data, str):
            data = urllib.parse.urlencode(data).encode()

        if data is not None:
            log("HTTP POST request to %s (POST data %s)" % (url, str(data)))
        else:
            log("HTTP GET request to %s" % url)

        response = urllib.request.urlopen(url, data).read().decode()
        log('Done')

        if auto_login and "<input type=submit value=' Login '>" in response:
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
        error = re.search(
            "<font color=#336699 size=3><br><b>(.*)<br><br><input type=submit value='Continue'><br><br>",
            html,
        )
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
            self.request("/cgi/logout", status="Logging out...", auto_login=False)
        except urllib.error.HTTPError as e:
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
            ('portset', port.num - 1),  # "portset" numbers from 0
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
        if vlan.internal_id is None:
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
        soup = bs4.BeautifulSoup(
            self.request("/cgi/device", status="Retrieving switch status..."),
            # Force using the lxml parser, since the builtin python
            # parser does not handle the incorrect HTML produced by the
            # switch correctly...
            'lxml',
        )

        try:
            self.parse_status(soup)
            self._emit('details_changed')
            self._emit('portlist_changed')
        except AttributeError:
            # Print HTML for debugging
            print(soup)
            raise

        self._emit('status_changed', None)

    def parse_status(self, soup):
        #####################################
        # Parse switch information
        #####################################
        h1 = soup.find(string="Switch Status")
        h1_table = h1.findParent('table')
        table = h1_table.findNext("table")

        rows = table.findAll('tr')

        for row in rows:
            tds = row.findAll('td')
            key = self.remove_html_tags(tds[0].text.strip())
            value = self.remove_html_tags(tds[1].text.strip())
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
        h1 = soup.find(string="PORT Status").parent
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
                speed = ths[0].text.strip()
                continue

            tds = row.findAll('td')
            if (tds):
                # Each row contains info for two ports, so iterate them
                for port_tds in (tds[0:5], tds[5:10]):
                    (num, speed_setting, flow_control, link_status, description) = [td.text.strip() for td in port_tds]
                    assert len(self.ports) == int(num) - 1, "Switch ports are not numbers consecutively?"

                    port = Port(self, int(num), speed, speed_setting, flow_control, link_status, description)
                    self.ports.append(port)

        #####################################
        # Parse vlan information
        #####################################
        h1 = soup.find(string="IEEE 802.1Q VLAN Settings").parent
        table = h1.findNext("table")

        rows = table.findAll('tr')

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
            dotq_id = int(tds[0].text.strip())

            # Create the vlan descriptor
            name = self.config['vlan_names'].get('vlan%d' % dotq_id, '')
            vlan = Vlan(self, internal_id, dotq_id, name)
            self.vlans.append(vlan)
            self.dotq_vlans[dotq_id] = vlan

            assert len(tds) == len(self.ports) + 1, "VLAN table has wrong number of ports?"
            for port in self.ports:
                # We skip td[0] (which contains a header), since we use
                # the 1-based port number
                text = tds[port.num].text.strip()
                if text == '':
                    vlan.ports[port] = Vlan.NOTMEMBER
                elif text == 'T':
                    vlan.ports[port] = Vlan.TAGGED
                elif text == 'U':
                    vlan.ports[port] = Vlan.UNTAGGED
                else:
                    sys.stderr.write('Ignoring unknown vlan/port status: %s \n' % text)

        self.max_vlan_internal_id = len(self.vlans)

        #####################################
        # Parse PVID information
        #####################################
        h1 = soup.find(string="IEEE 802.1Q PVID Table").parent
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
                    (num, pvid) = [td.text.strip() for td in port_tds]
                    if num:
                        # We set the internal value here, to prevent a
                        # change being generated in the changelist.
                        self.ports[int(num) - 1]._pvid = int(pvid)

    def remove_html_tags(self, data):
        p = re.compile(r'<.*?>')
        return p.sub('', data)
