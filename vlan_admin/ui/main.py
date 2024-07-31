import urwid

from .widgets import DisableEdit, KeypressAdapter, PortVlanMatrix, TopLine

from ..backends.fs726t import CommitException
from ..log import log

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
        ('active_port', normal_text + ',bold', normal_bg),
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

    def __init__(self, switch, status_on_start):
        self.switch = switch
        self.status_on_start = status_on_start
        self._overlay_widget = None
        super(Interface, self).__init__()

    def start(self):
        self.create_widgets()

        urwid.connect_signal(self.switch, 'status_changed', self.status_changed)

        self.loop = urwid.MainLoop(self.main_widget, palette=Interface.palette, unhandled_input=self.unhandled_input)
        # Register this idle callback before starting the mainloop, so
        # it gets called before the idle callback inside MainLoop that
        # redraws the screen.
        self.loop.event_loop.enter_idle(self.check_focus)
        self.loop.screen.run_wrapper(self.run)

    def check_focus(self):
        """
        Check which matrix cell has the current focus, and update the
        VLAN and Port details sections to reflect the current focus.
        """
        for w in self.main_widget.base_widget.get_focus_widgets():
            if w.base_widget is self.matrix:
                self.fill_details(Interface.port_attrs, self.port_widgets, self.matrix.focus_port)
                self.fill_details(Interface.vlan_attrs, self.vlan_widgets, self.matrix.focus_vlan)
                break

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

        def vlan_keypress_handler(widget, size, key):
            if key == 'delete':
                self.try_delete_vlan(widget.base_widget.vlan)
            else:
                return key
            return None

        self.matrix = PortVlanMatrix(self, self.switch, vlan_keypress_handler)
        matrix = urwid.Padding(TopLine(self.matrix, 'VLAN / Port mappings'), align='center')

        def update_matrix(switch):
            self.matrix.create_widgets()
            # Focus the matrix
            self.main_widget.base_widget.set_focus(matrix)

        # When the switch (re)loads the portlist, just recreate the widgets
        urwid.connect_signal(self.switch, 'portlist_changed', update_matrix)
        urwid.connect_signal(self.switch, 'vlanlist_changed', update_matrix)

        pile = urwid.Pile([('flow', switch_details),
                           ('flow', matrix),
                           ('flow', bottom),
                           ('flow', changelist),
                           ('flow', dbg)])

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
        self.main_widget = urwid.Filler(body, valign='top')

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
            overlay = urwid.Overlay(
                top_w=widget,
                bottom_w=self.main_widget,
                align='center',
                width=75,
                valign=('fixed top', 4),
                height=10,
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
        if self.status_on_start:
            self.switch.get_status()
        else:
            self.status_changed(None, None)

        log("Starting mainloop")
        self.loop.run()

    def create_details(self, attrs, widget_dict, gridflow=False):
        """
        Create a widget showing attributes of an object as specified in
        attrs. Is initially empty, call fill_details to fill in info.

        widget_dict should be an empty dict, which this method will
        fill. The same dict should be passed to fill_details.

        Returns the Widget created.
        """
        max_label_width = max([len(l) for c in attrs for (l, a, e) in c])

        top_columns = []
        for column in attrs:
            widgets = []
            for (label_text, attr, edit) in column:
                label = urwid.Text(label_text + ":")
                if edit:
                    widget = DisableEdit()
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
                if obj is None:
                    text = ''
                else:
                    text = str(getattr(obj, attr))
                w = widget_dict[attr]
                if edit:
                    w.obj = obj
                    w.set_edit_text(text)
                    w.disabled = (obj is None)
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
            if obj is not None:
                urwid.connect_signal(obj, 'details_changed', update_details)

                # Store the signal handler, since we need it to be identical
                # when disconnecting the signal later on.
                widget_dict['active_object_handler'] = update_details

    def fill_changelist(self, switch):
        if switch.changes:
            text = '\n'.join([str(c) for c in switch.changes])
        else:
            text = 'No changes'
        self.changelist.set_text(text)

    def unhandled_input(self, key):
        if key == 'q' or key == 'Q' or key == 'f10':
            self.switch.do_logout()
            raise urwid.ExitMainLoop()
        elif key in ['f11', 'c', 'C']:
            try:
                self.switch.commit_all()
            except CommitException as e:
                self.show_popup(str(e))
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

    def yesno_popup(self, text, yes_callback, no_callback=None):
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

    def input_popup(self, text, callback, cancel=None):
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
