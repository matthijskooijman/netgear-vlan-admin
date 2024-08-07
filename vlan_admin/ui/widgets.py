import urwid

from ..backends.common import Vlan


class DisableEdit(urwid.Edit):
    def __init__(self, *args, **kwargs):
        super(DisableEdit, self).__init__(*args, **kwargs)
        self.disabled = False

    def selectable(self):
        return not self.disabled

    def render(self, size, focus=False):
        # Pretend we're always unfocused when we're disabled, to prevent
        # rendering the cursor
        return super(DisableEdit, self).render(size, not self.disabled and focus)


class PortVlanMatrix(urwid.WidgetWrap):
    """
    Widget that displays a matrix of ports versus vlans and allows to
    edit the vlan memberships.
    """
    def __init__(self, interface, switch, vlan_keypress_handler):
        self.interface = interface
        self.switch = switch
        self.vlan_keypress_handler = vlan_keypress_handler

        super(PortVlanMatrix, self).__init__(None)

        self.create_widgets()

    def get_focus_attr(self, attrname):
        if self._w.focus_position == 0:
            widget = self._w.focus.focus.base_widget
        else:
            widget = self._w.focus.focus.focus.base_widget
        return getattr(widget, attrname, None)

    # Return the focused vlan or port
    focus_vlan = property(lambda self: self.get_focus_attr('vlan'))
    focus_port = property(lambda self: self.get_focus_attr('port'))

    def create_widgets(self):
        # We build a matrix using a Column of Piles, since that allows
        # synchronized horizontal scrolling (for e.g. 48-port switches).
        # Originally, the structure was transposed, which has the
        # advantage that a Pile keeps the horizontal cursor position
        # when moving focus vertically, which Columns does not do when
        # moving horizontally, but this was instead fixed by copying the
        # focus position to all columns in keypress() below.

        # Find out the maximum vlan name length, so we can make all the
        # vlan names in the first column have the same width. Ensure
        # it's always 20 characters wide.
        self.vlan_header_width = max([20] + [len(v.name) for v in self.switch.vlans]) + 10

        header_column = [urwid.Text("")]

        # Create a header column with vlan names
        for vlan in self.switch.vlans:
            widget = urwid.Text("")

            def update_vlan_header(vlan_header, vlan):
                vlan_header.set_text("%4s: %s" % (vlan.dotq_id, vlan.name))
            update_vlan_header(widget, vlan)
            urwid.connect_signal(vlan, 'details_changed', update_vlan_header, weak_args=[widget])

            widget = KeypressAdapter(widget, self.vlan_keypress_handler)
            # For the focus_vlan attribute
            widget.base_widget.vlan = vlan

            header_column.append(urwid.AttrMap(widget, None, 'focus'))

        # Create a column for each port
        port_columns = []
        for port in self.switch.ports:
            column = []

            widget = urwid.Text(" %02d " % port.num)
            if port.up:
                widget = urwid.AttrMap(widget, 'active_port', None)
            column.append(widget)

            for vlan in self.switch.vlans:
                widget = urwid.Text("")

                widget = PortVlanWidget(self.interface, port, vlan)
                column.append(
                    widget
                )
            port_columns.append((4, urwid.Pile(column)))

        # Use two nested Columns, which causes the inner Columns with
        # just the ports to drop ports on the left when the focus would
        # be out of view, which keeps the vlan header name in view
        self._w = urwid.Columns([
            (self.vlan_header_width, urwid.Pile(header_column)),
            urwid.Columns(port_columns),
        ])

    def keypress(self, size, key):
        ret = super(PortVlanMatrix, self).keypress(size, key)

        # Copy the vertical focus position of the focused column to
        # all other columns, so horizontal navigation keeps the
        # vertical position
        # It would be nicer to do this in a focus change callback,
        # but it seems MonitoredFocusList does have a callback, but
        # there can be just one callback and it is already used by
        # Columns
        if ret is None:
            ports, _ = self._w.contents[1]
            focused_vlan = ports.focus.focus_position
            for pile, _ in ports.contents:
                pile.focus_position = focused_vlan

        return ret

    # Always mark ourselves as selectable, even if we are still empty at
    # initialization, since Columns and Pile cache their contents
    # selectability once during init only. This might be a bug, but this
    # is an effective workaround.
    def selectable(self):
        return True


class PortVlanWidget(urwid.Widget):
    """
    Class to display and edit a port / vlan combination.
    """

    _sizing = frozenset(['flow'])

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
        text = " " * ((cols - 2) // 2)
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

        text += " " * (cols - 2 - ((cols - 2) // 2))

        return urwid.TextCanvas([text.encode()], [[(attr, len(text))]])

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
        if text is not None:
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
    def __init__(self, original_widget, title=''):
        super(TopLine, self).__init__(
            original_widget, title,
            tlcorner=' ', trcorner=' ', blcorner=' ',
            brcorner=' ', rline=' ', lline=' ', bline=' ')


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
