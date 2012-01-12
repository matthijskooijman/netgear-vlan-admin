#!/usr/bin/python

import urllib, urllib2
import re
import sys
import os.path
import pickle
import configobj
import validate
from BeautifulSoup import BeautifulSoup
import urwid
from StringIO import StringIO

config_filename = os.path.expanduser("~/.config/vlan-admin.conf")

running = False
ui = None

# Some machinery to load a cached version of the settings, to speed up
# debugging.
write = False
load = False

def log(text):
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
    """

    def merge_with(self, other):
        if (isinstance(other, VlanNameChange) and
            other.what == self.what):

            if (self.how == other.old):
                # This changes cancels the other change, remove them
                # both
                return (None, None)
            else:
                # This change replaces the other change. Note this actually
                # means this changes ends up in the position of the other
                # change in the changelist.
                self.old = other.old
                return (None, self)

        # In all other cases, keep all of them
        return (self, other)

    def __unicode__(self):
        return 'Changing vlan %d name to: %s' % (self.what.dotq_id, self.how)

class PortDescriptionChange(Change):
    """
    Record the change of a port description. Constructor arguments:
    what: Port object
    how: new description (string)
    """

    def merge_with(self, other):
        if (isinstance(other, PortDescriptionChange) and
            other.what == self.what):

            if (self.how == other.old):
                # This changes cancels the other change, remove them
                # both
                return (None, None)
            else:
                # This change replaces the other change. Note this actually
                # means this changes ends up in the position of the other
                # change in the changelist.
                self.old = other.old
                return (None, self)

        # In all other cases, keep all of them
        return (self, other)

    def __unicode__(self):
        return 'Changing port %d description to: %s' % (self.what.num, self.how)


class Vlan(object):
    # Constants for the PortVLanMembershipChange. The values are also
    # the ones used by the FS726 HTTP interface.
    NOTMEMBER = 0
    TAGGED = 1
    UNTAGGED = 2

    def __init__(self, switch, internal_id, dotq_id):
        """
        Represents a vlan, consisting of an internal id (used to
        identify the vlan in the switch), the 802.11q id associated with
        the vlan and a name.
        """
        self.switch = switch
        self.internal_id = internal_id
        self.dotq_id = dotq_id
        # Use the dotq_id, since that's constant over time
        self.config_key = "vlan%d" % dotq_id
        # Map a Port object to either NOTMEMBER, TAGGED or UNTAGGED
        self.ports = {}

        try:
            self._name = self.switch.config['vlan_names'][self.config_key]
        except KeyError:
            self._name = ''


    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if value != self._name:
            self.switch.queue_change(VlanNameChange(self, value, self._name))
            self._name = value

    def __repr__(self):
        return u"VLAN %s: %s (802.11q ID %s)" % (self.internal_id, self.name, self.dotq_id)

class Port(object):
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

        self.pvid = None # Should be set afterwards

        super(Port, self).__init__()

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, value):
        if value != self._description:
            self.switch.queue_change(PortDescriptionChange(self, value, self._description))
            self._description = value

    def __repr__(self):
        return u"Port %s: %s (speed: %s, speed setting: %s, flow control: %s, link status = %s)" % (self.num, self.description, self.speed, self.speed_setting, self.flow_control, self.link_status)

class FS726T(object):
    # Autoregister signals
    __metaclass__ = urwid.MetaSignals
    signals = ['changelist_changed', 'details_changed', 'portlist_changed', 'status_changed']

    def __init__(self, address = None, password = None, config = None):
        self.address = address
        self.password = password
        self.ports = {}
        self.vlans = {}
        self.config = config
        self.changes = []

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

    def queue_change(self, new_change):
        # Make a new changes list to prevent issues with inline
        # modification (while looping the list)
        new_changes = []
        for change in self.changes:
            if new_change:
                # As long as the new_change hasn't removed itself yet,
                # try to merge it with each change
                (new_change, change) = new_change.merge_with(change)

            # If the merging does not remove change, keep it in the list
            if not change is None:
                new_changes.append(change)

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

        for change in self.changes:
            if isinstance(change, PortDescriptionChange):
                self.commit_port_description_change(change.what, change.how)
            elif isinstance(change, VlanNameChange):
                self.config['vlan_names'][change.what.config_key] = change.how
                write_config = True

        if write_config:
            self.config.write()

        self.changes = []
        self._emit('changelist_changed')
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
        self.ports = {}
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

                    port = Port(self, int(num), speed, speed_setting, flow_control, link_status, description)
                    self.ports[int(num)] = port

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

        self.vlans = {}
        for row in vlan_rows:
            tds = row.findAll('td')
            # We assume the vlans are listed in order of their internal
            # id (e.g., order of creation)
            internal_id = len(self.vlans)
            # The first td shows the 802.11q id
            dotq_id = int(tds[0].text)

            # Create the vlan descriptor
            vlan = Vlan(self, internal_id, dotq_id)
            self.vlans[internal_id] = vlan

            for portnum in range(1, len(tds)):
                td = tds[portnum]
                port = self.ports[portnum]
                if td.text == '':
                    vlan.ports[port] = Vlan.NOTMEMBER
                elif td.text == 'T':
                    vlan.ports[port] = Vlan.TAGGED
                elif td.text == 'U':
                    vlan.ports[port] = Vlan.UNTAGGED
                else:
                    sys.stderr.write('Ignoring unknown vlan/port status: %s \n' % td.vlaue)

class PortVlanMatrix(urwid.WidgetWrap):
    """
    Widget that displays a matrix of ports versus vlans and allows to
    edit the vlan memberships.
    """
    def __init__(self, switch, focus_change):
        self.switch = switch
        self.focus_change = focus_change

        super(PortVlanMatrix, self).__init__(None)

        self.create_widgets(switch)

        # When the switch (re)loads the portlist, just recreate the widgets
        urwid.connect_signal(switch, 'portlist_changed', self.create_widgets)

    def create_widgets(self, switch):
        # We build a matrix using a Pile of Columns. This allows us to
        # properly navigate through the matrix. We can't inverse this
        # (using a Columns of Piles), since there is no code to
        # preserver the vertical "preferred cursor position", only
        # for horizontal.

        # Find out the maximum vlan name length, so we can make all the
        # vlan names in the first column have the same width. Ensure
        # it's always 20 characters wide.
        self.vlan_header_width = max([20] + [len(v.name) for v in self.switch.vlans.values()]) + 10

        rows = []

        # Create the header row, containing port numbers
        row = [('fixed', self.vlan_header_width, urwid.Text(""))]
        for port in switch.ports.values():
            row.append(
                ('fixed', 4, urwid.Text(" %02d " % port.num))
            )
        rows.append(urwid.Columns(row))

        # Create a row for each vlan
        for vlan in switch.vlans.values():
            edit = urwid.Edit("%4s: " % vlan.dotq_id, vlan.name)
            row = [('fixed', self.vlan_header_width, edit)]
            def vlan_title_change(widget, text):
                widget.vlan.name = text

            # Save the vlan in the widget for the callback
            edit.vlan = vlan
            urwid.connect_signal(edit, 'change', vlan_title_change)
            for port in switch.ports.values():
                widget = PortVlanWidget(port, vlan)
                urwid.connect_signal(widget, 'focus', self.focus_change)
                widget = urwid.AttrMap(widget, None, 'focus')
                row.append(
                    ('fixed', 4, widget)
                )
            rows.append(urwid.Columns(row))

        self._w = urwid.Pile(rows)

    # We have a fixed size
    def sizing(self):
        return set([urwid.FIXED])

    def pack(self, size, focus=False):
        # Calculate our fixed size
        columns = self.vlan_header_width + len(self.switch.ports.values()) * 4
        # Delegate the rows calculation to the Pile
        rows = self.rows((columns,))
        return (columns, rows)



class PortVlanWidget(urwid.FlowWidget):
    """
    Class to display and edit a port / vlan combination.
    """

    def __init__(self, port, vlan):
        super(PortVlanWidget, self).__init__()
        self._selectable = True
        self.port = port
        self.vlan = vlan

    def keypress(self, size, key):
        return key

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

# Support vim key bindings in the default widgets
urwid.command_map['j'] = 'cursor down'
urwid.command_map['k'] = 'cursor up'
urwid.command_map['h'] = 'cursor left'
urwid.command_map['l'] = 'cursor right'

class Interface(object):
    normal_text = 'light gray'
    untagged_text = 'light blue'
    tagged_text = 'dark red'
    normal_bg = 'black'
    focus_bg = 'light gray'
    palette = [
        ('header', 'black', 'light gray'),
        ('none', normal_text, normal_bg),
        ('tagged', tagged_text, normal_bg),
        ('untagged', untagged_text, normal_bg),
        ('none_focus', normal_text, focus_bg),
        ('tagged_focus', tagged_text, focus_bg),
        ('untagged_focus', untagged_text, focus_bg),
        ('status', 'white', 'dark blue'),
    ]

    # (Label, attribute, editable?)
    port_attrs = [[
        ('Port number', 'num', False),
        ('Description', 'description', True),
        ('Port speed', 'speed', False),
        ('Configured speed', 'speed_setting', False),
        ('Flow control', 'flow_control', False),
        ('Link', 'link_status', False),
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
        self.showing_popup = False
        super(Interface, self).__init__()

    def start(self):
        self.create_widgets()

        urwid.connect_signal(switch, 'status_changed', self.status_changed)

        self.loop = urwid.MainLoop(self.overlay_widget, palette=Interface.palette, unhandled_input=self.unhandled_input)
        self.loop.screen.run_wrapper(self.run)

    def create_widgets(self):
        self.header = header = urwid.Text("Connected to %s" % self.switch.address, align='center')
        header = urwid.AttrMap(header, 'header')

        self.port_widgets = {}
        self.vlan_widgets = {}
        self.switch_widgets = {}
        #port_list = self.create_port_list()
        port_details = self.create_details(Interface.port_attrs, self.port_widgets)
        vlan_details = self.create_details(Interface.vlan_attrs, self.vlan_widgets)

        switch_details = self.create_details(Interface.switch_attrs, self.switch_widgets, True)

        def fill_switch_details(switch):
            self.fill_details(Interface.switch_attrs, self.switch_widgets, switch)

        urwid.connect_signal(switch, 'details_changed', fill_switch_details)
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
        urwid.connect_signal(switch, 'changelist_changed', self.fill_changelist)
        self.fill_changelist(switch)

        def matrix_focus_change(widget):
            self.fill_details(Interface.port_attrs, self.port_widgets, widget.port)
            self.fill_details(Interface.vlan_attrs, self.vlan_widgets, widget.vlan)

        matrix = urwid.Padding(PortVlanMatrix(self.switch, matrix_focus_change), align='center')
        matrix = TopLine(matrix, 'VLAN / Port mappings')

        body = urwid.Pile([('flow', switch_details), 
                           ('flow', matrix),
                           ('flow', bottom),
                           ('flow', changelist),
                           ('flow', dbg),
                          ])
        self.main_widget = urwid.Filler(body, valign = 'top')

        self.status_widget = urwid.Text('Starting...', align='center')
        status = urwid.Filler(self.status_widget)
        status = urwid.LineBox(status)
        status = urwid.AttrMap(status, 'status')
        self.overlay_widget = urwid.Overlay(
            top_w = status,
            bottom_w = self.main_widget,
            align = 'center',
            width = 50,
            valign = ('fixed top', 4),
            height = 10,
        )

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


    def fill_changelist(self, switch):
        if switch.changes:
            text = u'\n'.join([unicode(c) for c in switch.changes])
        else:
            text = 'No changes'
        self.changelist.set_text(text)

    def create_port_list(self):
        ports = urwid.SimpleListWalker([])
        for port in self.switch.ports.values():
            w = PortWidget(port)
            # Use a different attribute when focused
            w = urwid.AttrMap(w, None, 'focus')
            ports.append(w)

        urwid.connect_signal(ports, 'modified',
            lambda: self.fill_port_details(ports.get_focus()[0]._get_base_widget().port)
        )

        return urwid.ListBox(ports)

    def unhandled_input(self, key):
        if key == 'q' or key == 'Q' or key == 'f10':
            raise urwid.ExitMainLoop()
        elif self.showing_popup:
            # Any keypress will hide the popup
            self.hide_popup()
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
            self.status_widget.set_text(new_status)
            self.loop.widget = self.overlay_widget
        else:
            self.loop.widget = self.main_widget

        # Force a screen redraw (in case we're called from a keypress
        # handler which takes a while to copmlete, for example).
        self.loop.draw_screen()

    def show_popup(self, text):
        # We just abuse the status widget for showing a modal popup
        self.status_changed(None, text + "\n\n\nPress any key...")
        self.showing_popup = True

    def hide_popup(self):
        self.status_changed(None, None)
        self.showing_popup = False

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
