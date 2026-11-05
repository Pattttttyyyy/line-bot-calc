from dataclasses import replace
from unittest.mock import MagicMock
import unittest
from .flow import Refused
from .images import ImageRef,receive_image,prepare_command


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.image=ImageRef('group:FAKE','admin','image-id',900)
        self.store=MagicMock();self.store.latest.return_value=self.image
        self.policy=dict(admin=True,enabled=True,group_allowed=True)
    def command(self,**extra):
        return prepare_command('扣點3290','group:FAKE','admin','command-id',950,self.store,now=1000,**dict(self.policy,**extra))
    def test_image_only_saves_metadata_no_payment(self):
        text=receive_image(self.image,self.store,now=1000,**self.policy)
        self.store.save_once.assert_called_once_with(self.image)
        self.assertIn('扣點3290',text)
    def test_nonadmin_cannot_store_or_read(self):
        with self.assertRaises(Refused):receive_image(self.image,self.store,now=1000,**dict(self.policy,admin=False))
        with self.assertRaises(Refused):self.command(admin=False)
        self.store.save_once.assert_not_called();self.store.latest.assert_not_called()
    def test_default_disabled_before_store_access(self):
        with self.assertRaises(Refused):receive_image(self.image,self.store,now=1000,admin=True,group_allowed=True)
        self.store.save_once.assert_not_called()
    def test_private_chat_and_disallowed_group_refused(self):
        with self.assertRaises(Refused):receive_image(replace(self.image,group='user:FAKE'),self.store,now=1000,**self.policy)
        with self.assertRaises(Refused):self.command(group_allowed=False)
        self.store.latest.assert_not_called();self.store.save_once.assert_not_called()
    def test_stale_replay_does_not_refresh_image_time(self):
        with self.assertRaises(Refused):receive_image(self.image,self.store,now=1400,**self.policy)
        self.store.save_once.assert_not_called()
    def test_future_and_nonfinite_time_rejected(self):
        for timestamp in (1001,float('nan'),float('inf'),True):
            with self.subTest(timestamp=timestamp),self.assertRaises(Refused):receive_image(replace(self.image,received_at=timestamp),self.store,now=1000,**self.policy)
        self.store.save_once.assert_not_called()
    def test_command_pins_image_id_and_points(self):
        request=self.command()
        self.assertEqual(request.points,3290);self.assertEqual(request.image_message,'image-id')
        self.assertEqual(request.message,'command-id')
        self.store.latest.assert_called_once_with('group:FAKE','admin')
    def test_missing_image_rejected(self):
        self.store.latest.return_value=None
        with self.assertRaises(Refused):self.command()
    def test_cross_group_or_admin_image_refused(self):
        for extra in (dict(group='group:OTHER'),dict(operator='other-admin')):
            self.store.latest.return_value=replace(self.image,**extra)
            with self.subTest(extra=extra),self.assertRaises(Refused):self.command()
    def test_expired_or_later_image_cannot_be_selected(self):
        for timestamp in (600,960):
            self.store.latest.return_value=replace(self.image,received_at=timestamp)
            with self.subTest(timestamp=timestamp),self.assertRaises(Refused):self.command()

if __name__=='__main__':unittest.main()
