#!/usr/bin/python

import urllib, urllib2
import re
import sys
import os.path
import pickle
import configobj
from BeautifulSoup import BeautifulSoup
import urwid

config_filename = os.path.expanduser("~/.config/vlan-admin.conf")

debug = urwid.Text('')
running = False

def log(text):
    # Note: This discards any existing markup
    debug.set_text(debug.text + text + "\n")
    if not running:
        print(text)

class LoginException(Exception):
    pass

class Vlan(object):
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
        self._config_key = "vlan%d" % dotq_id
        try:
            self._name = self.switch.config['vlan_names'][self._config_key]
        except KeyError:
            self._name = ''


    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if value != self._name:
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
        self.tagged_vlans = {}
        self.untagged_vlans = {}

        super(Port, self).__init__()

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, value):
        if value != self._description:
            self._description = value

    def __repr__(self):
        return u"Port %s: %s (speed: %s, speed setting: %s, flow control: %s, link status = %s)" % (self.num, self.description, self.speed, self.speed_setting, self.flow_control, self.link_status)

class FS726T(object):

    def __init__(self, address = None, password = None, config = None):
        self.address = address
        self.password = password
        self.ports = None
        self.vlans = None
        self.config = config

        super(FS726T, self).__init__()

    def request(self, path, data = None):
        url = "http://%s%s" % (self.address, path)
        log("HTTP request to %s" % url)
        log("POST data: %s" % str(data))
        response = urllib2.urlopen(url, data).read()
        log('Done')

        if "<input type=submit value=' Login '>" in response:
            self.do_login()
            return self.request(path, data)
        return response

    def do_login(self):
        """
        Log into this device. Be sure to always call do_logout as well,
        to prevent other users from getting locked out.
        """

        # Apparently, login is done on an IP basis, not tracked by cookies.
        data = urllib.urlencode({
            'passwd': self.password,
            'post_url': "/cgi/device",
        })
        html = self.request("/cgi/device", data)

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
            self.request("/cgi/logout")
        except urllib2.HTTPError,e:
            if e.code == 404:
                sys.stderr.write("Ignoring logout error, we're probably not logged in.\n")
            else:
                raise

    def get_status(self):
        soup = BeautifulSoup(self.request("/cgi/device"), convertEntities=BeautifulSoup.HTML_ENTITIES)

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
                if td.text == '':
                    continue
                port = self.ports[portnum]
                if td.text == 'T':
                    port.tagged_vlans[vlan.internal_id] = vlan
                elif td.text == 'U':
                    port.untagged_vlans[vlan.internal_id] = vlan
                else:
                    sys.stderr.write('Ignoring unknown vlan/port status: %s \n' % td.vlaue)

class PortVlanMatrix(urwid.WidgetWrap):
    """
    Widget that displays a matrix of ports versus vlans and allows to
    edit the vlan memberships.
    """
    def __init__(self, switch, focus_change):
        self.switch = switch
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
                urwid.connect_signal(widget, 'focus', focus_change)
                widget = urwid.AttrMap(widget, None, 'focus')
                row.append(
                    ('fixed', 4, widget)
                )
            rows.append(urwid.Columns(row))

        pile = urwid.Pile(rows)

        super(PortVlanMatrix, self).__init__(pile)

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
        if self.vlan.internal_id in self.port.tagged_vlans:
            text += "TT"
            attr = "tagged"
        elif self.vlan.internal_id in self.port.untagged_vlans:
            text += "UU"
            attr = "untagged"
        else:
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
        super(Interface, self).__init__()

    def run(self):
        self.header = header = urwid.Text("Connected to %s" % self.switch.address, align='center')
        header = urwid.AttrMap(header, 'header')

        self.port_widgets = {}
        self.vlan_widgets = {}
        self.switch_widgets = {}
        #port_list = self.create_port_list()
        port_details = self.create_details(Interface.port_attrs, self.port_widgets)
        vlan_details = self.create_details(Interface.vlan_attrs, self.vlan_widgets)

        switch_details = self.create_details(Interface.switch_attrs, self.switch_widgets, True)
        self.fill_details(Interface.switch_attrs, self.switch_widgets, self.switch)
        switch_details = TopLine(switch_details, title="Connected switch")

        bottom = urwid.Columns([
            port_details,
            vlan_details,
        ])
        bottom = TopLine(bottom, 'Details')

        dbg = TopLine(debug, 'Debug')

        def matrix_focus_change(widget):
            self.fill_details(Interface.port_attrs, self.port_widgets, widget.port)
            self.fill_details(Interface.vlan_attrs, self.vlan_widgets, widget.vlan)

        matrix = urwid.Padding(PortVlanMatrix(self.switch, matrix_focus_change), align='center')
        matrix = TopLine(matrix, 'VLAN / Port mappings')

        body = urwid.Pile([('flow', switch_details), 
                           ('flow', matrix),
                           ('flow', bottom),
                           ('flow', dbg),
                          ])
        body = urwid.Filler(body, valign = 'top')

        loop = urwid.MainLoop(body, palette=Interface.palette, unhandled_input=self.unhandled_input)
        log("Starting mainloop")
        loop.run()

    def create_switch_details(self):
        max_label_width = max([len(l) for (l, a, e) in Interface.switch_attrs])

        widgets = []
        for (label_text, attr, edit) in Interface.switch_attrs:
            label = urwid.Text(label_text + ":")
            text = str(getattr(self.switch, attr))
            if edit:
                value = urwid.Edit(text)
            else:
                value = urwid.Text(text)

            columns = urwid.Columns([
                ('fixed', max_label_width + 4, label),
                value,
            ])

            widgets.append(columns)

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
        if key == 'q' or key == 'Q':
            raise urwid.ExitMainLoop()

        log("Unhandled keypress: %s" % str(key))

        return False

def remove_html_tags(data):
    p = re.compile(r'<.*?>')
    return p.sub('', data)

# Some machinery to load a cached version of the settings, to speed up
# debugging.
write = True
load = not write

# Create the switch object
config = configobj.ConfigObj(infile = config_filename, create_empty = True, encoding='UTF8')
if load:
    # Load the switch object from a debug file
    f = open('switch.dump', 'r')
    switch = pickle.load(f)
    switch.config.reload()
else:
    # Create a new switch object and get the status from the switch
    switch = FS726T('192.168.1.253', 'password', config)
    switch.get_status()

if write:
    # Dump the switch object
    f = open('switch.dump', 'w')
    pickle.dump(switch, f)

# Create an interface for the switch
ui = Interface(switch)
running = True
ui.run()

# When quitting, write out the configuration
switch.config.write()
