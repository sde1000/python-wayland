from unittest import TestCase

import wayland.protocol
import wayland.client

from tests.data import sample_protocol
import io

class TestProtocol(TestCase):
    """Test wayland.protocols"""

    @classmethod
    def setUpClass(cls):
        # A wayland.protocol.Protocol is immutable once loaded, so we
        # can load the test data once and use it for all the tests.
        f = io.StringIO(sample_protocol)
        cls.w = wayland.protocol.Protocol(f)

    def test_protocol_name(self):
        self.assertEqual(self.w.name, "wayland")

    def test_protocol_copyright(self):
        self.assertIsNotNone(self.w.copyright)

    def test_protocol_interfaces(self):
        self.assertIsInstance(self.w.interfaces, dict)
        self.assertIn("wl_display", self.w.interfaces)
        self.assertIsInstance(self.w['wl_display'], wayland.protocol.Interface)

    def test_duplicate_interface_name(self):
        f = io.StringIO(sample_protocol)
        with self.assertRaises(wayland.protocol.DuplicateInterfaceName):
            # Try to load the test protocol with itself as a parent;
            # should fail on the first interface declaration and leave
            # the parent unchanged
            wayland.protocol.Protocol(f, parent=self.w)

    def test_interface_name(self):
        for i in self.w.interfaces.keys():
            self.assertEqual(self.w[i].name, i)

    def test_interface_version(self):
        for i in self.w.interfaces.keys():
            self.assertIsInstance(self.w[i].version, int)
