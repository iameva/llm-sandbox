"""Local checks for capture parsing and missing-control failure handling."""
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dns_observation import Observation, packet_records, query


def pcap(packets):
    header = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 256, 1)
    return header + b''.join(struct.pack('<4I', 0, 0, len(p), len(p))+p for p in packets)


class CaptureTests(unittest.TestCase):
    def test_truncation_is_rejected(self):
        with self.assertRaises(ValueError):
            list(packet_records(pcap([b'packet'])[:-1]))

    def evaluate(self, labels):
        with tempfile.TemporaryDirectory() as directory:
            observation = Observation(Path(directory))
            observation.process = Mock()
            observation.process.poll.return_value = None
            observation.process.wait.return_value = 124
            observation.output = Mock()
            observation.error = Mock()
            (Path(directory)/'dns-observation.log').write_text('0 packets dropped by kernel')
            (Path(directory)/'dns-observation.pcap').write_bytes(
                pcap([query(observation.tag, label) for label in labels]))
            with patch.object(observation, 'send_control'):
                return observation.finish()

    def test_missing_positive_control_is_inconclusive(self):
        with self.assertRaisesRegex(RuntimeError, 'positive controls'):
            self.evaluate(['before'])

    def test_guest_packet_fails_boundary_check(self):
        self.assertFalse(self.evaluate(['before', 'after', 'a-builtin'])
                         ['guest_dns_absent_from_host_capture'])

    def test_both_controls_without_guest_packets_pass(self):
        self.assertTrue(self.evaluate(['before', 'after'])
                        ['guest_dns_absent_from_host_capture'])


if __name__ == '__main__':
    unittest.main()
