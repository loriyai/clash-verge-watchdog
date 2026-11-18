"""Recovery sequence, cancellation and rest regressions."""
import time
from unittest.mock import patch
import test_controller
from test_controller import snapshot
import unittest
from clash_watchdog.recovery import Recovery
from clash_watchdog.api import Cancelled


class RecoveryTests(unittest.TestCase):
    make = test_controller.ControllerTests.make
    seed = test_controller.ControllerTests.seed
    setUp = test_controller.ControllerTests.setUp
    def test_tun_success_avoids_core_and_software_restart(self):
        m,store,p,api,clock,events=self.make()
        self.seed(m)
        api.outcome="success"
        m._try_restart(snapshot().resolve("group"))
        self.assertEqual(0,p.restarts)
        self.assertEqual(0,m.engine.consecutive_failures)
        self.assertFalse(m.state["tun_restore_pending"])

    def test_complete_failure_chain_then_rest_and_resume(self):
        m,store,p,api,clock,events=self.make()
        self.seed(m)
        toggles=[]
        p.set_tun=lambda enabled,cancel=None:toggles.append(enabled)
        m._try_restart(snapshot().resolve("group"))
        self.assertEqual(4,toggles.count(False))
        self.assertEqual(1,p.restarts)
        self.assertEqual(1,len(m.state["software_attempts"]))
        self.assertEqual(clock()+60,m.state["rest_until"])
        before=api.diagnoses
        m._network_tick(clock())
        self.assertEqual(before,api.diagnoses)
        m.rest_deadline=time.monotonic()-1
        api.outcome="success"
        m._network_tick(clock())
        self.assertEqual(before+1,api.diagnoses)
        self.assertEqual(0,m.state["rest_until"])

    def test_cancel_during_tun_off_restores_enabled(self):
        m,store,p,api,clock,events=self.make()
        toggles=[]
        p.set_tun=lambda enabled,cancel=None:toggles.append(enabled)
        r=Recovery(m,m.cancel,snapshot().resolve("group"))
        with patch.object(r,"wait",side_effect=Cancelled("stop")):
            with self.assertRaises(Cancelled):
                r.tun_cycles()
        self.assertEqual([False,True],toggles)
        self.assertFalse(m.state["tun_restore_pending"])

    def test_restore_failure_keeps_durable_pending_flag(self):
        m,store,p,api,clock,events=self.make()
        def change(enabled,cancel=None):
            if enabled:raise RuntimeError("unavailable")
        p.set_tun=change
        with self.assertRaises(RuntimeError):
            Recovery(m,m.cancel,snapshot().resolve("group")).tun_cycles()
        self.assertTrue(store.saved[-1]["tun_restore_pending"])


    def test_unknown_diagnostic_does_not_restart_core(self):
        from clash_watchdog.api import ApiError
        m,store,p,api,clock,events=self.make()
        self.seed(m)
        api.error=ApiError("controller", "unknown")
        m._try_restart(snapshot().resolve("group"))
        self.assertEqual(0,p.restarts)
        self.assertEqual([],m.state["software_attempts"])
        self.assertTrue(m.running)

    def test_rest_tun_off_requests_restore_before_new_countdown(self):
        m,store,p,api,clock,events=self.make()
        m.state["rest_until"]=clock()+60
        api.tun_enabled=lambda cancel=None:False
        m._network_tick(clock())
        self.assertTrue(m.state["tun_restore_pending"])
        self.assertTrue(m.state["rest_after_tun"])
        self.assertEqual(0,m.state["rest_until"])
