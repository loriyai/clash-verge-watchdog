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

    def test_missing_gui_can_launch_while_tun_restore_pending(self):
        m,store,p,api,clock,events=self.make(automatic_launch=True)
        m.state.update(tun_restore_pending=True,rest_after_tun=True)
        p.presence="missing"
        for at in (1000,1005,1010):
            clock.now=at
            m._app_tick(at)
        self.assertEqual(1,p.launches)
        self.assertTrue(m.state["tun_restore_pending"])
        p.presence="running"
        m._network_tick(clock())
        self.assertFalse(m.state["tun_restore_pending"])
        self.assertEqual(clock()+60,m.state["rest_until"])

    def test_disabled_auto_launch_still_respected_when_pending(self):
        m,store,p,api,clock,events=self.make(automatic_launch=False)
        m.state["tun_restore_pending"]=True
        p.presence="missing"
        for at in (1000,1005,1010):
            clock.now=at
            m._app_tick(at)
        self.assertEqual(0,p.launches)

    def test_tun_initially_off_stays_off_through_entire_chain(self):
        m,store,p,api,clock,events=self.make()
        self.seed(m)
        api.tun_enabled=lambda cancel=None:False
        actions=[]
        p.set_tun=lambda enabled,cancel=None:actions.append(enabled)
        m._try_restart(snapshot().resolve("group"))
        self.assertEqual(1,p.restarts)
        self.assertEqual(1,len(m.state["software_attempts"]))
        self.assertTrue(actions)
        self.assertNotIn(True,actions)
        self.assertFalse(m.state["recovery_tun_enabled"])
        self.assertEqual(clock()+60,m.state["rest_until"])
        m._network_tick(clock())
        self.assertFalse(m.state["tun_restore_pending"])

    def test_failed_state_commit_does_not_publish_transition(self):
        m,store,p,api,clock,events=self.make()
        store.fail_save=True
        with self.assertRaises(OSError):
            m.enter_rest()
        self.assertEqual(0,m.state["rest_until"])
        self.assertEqual(0,m.rest_deadline)

    def test_software_restart_limit_keeps_diagnosis_running(self):
        m,store,p,api,clock,events=self.make()
        m.state["software_attempts"]=[clock()-20,clock()-10]
        self.assertFalse(m.reserve_software_restart())
        self.assertTrue(m.running)
        self.assertEqual(2,len(m.state["software_attempts"]))

    def test_wall_clock_forward_jump_does_not_skip_active_rest(self):
        m,store,p,api,clock,events=self.make()
        m.enter_rest()
        before=m.rest_deadline
        clock.advance(3600)
        m._network_tick(clock())
        self.assertEqual(before,m.rest_deadline)
        self.assertGreater(m.state["rest_until"],0)
        self.assertEqual(0,api.diagnoses)

    def test_finish_rest_clears_stage_and_probe_is_immediately_due(self):
        m,store,p,api,clock,events=self.make()
        m.enter_rest()
        m.finish_rest(clock())
        self.assertEqual("",m.state["recovery_stage"])
        self.assertTrue(m.engine.need_probe(clock()))
        self.assertEqual(0,m.engine.consecutive_failures)

    def test_restart_reservation_preserves_cooldown_inside_cycle(self):
        m,store,p,api,clock,events=self.make()
        self.assertEqual("ready",m.engine.record_recovery_restart(clock()))
        self.assertEqual("cooldown",m.engine.record_recovery_restart(clock()+1))
        self.assertEqual(1,len(m.engine.export_ledger(clock())["attempts"]))

    def test_software_not_ready_leaves_resumable_wait(self):
        m,store,p,api,clock,events=self.make(automatic_launch=True)
        self.seed(m)
        with patch("clash_watchdog.recovery.Recovery.ready", return_value=False):
            m._try_restart(snapshot().resolve("group"))
        self.assertTrue(m.state["tun_restore_pending"])
        self.assertTrue(m.state["rest_after_tun"])
        self.assertEqual(0,m.state["rest_until"])
        p.presence="missing"
        for at in (1000,1005,1010):
            clock.now=at
            m._app_tick(at)
        self.assertEqual(1,p.launches)
        p.presence="running"
        m._network_tick(clock())
        self.assertFalse(m.state["tun_restore_pending"])
        self.assertGreater(m.state["rest_until"],clock())

    def test_pending_non_tun_mode_restores_false_after_relaunch(self):
        m,store,p,api,clock,events=self.make()
        m.state.update(tun_restore_pending=True,rest_after_tun=True,recovery_tun_enabled=False)
        api.tun_enabled=lambda cancel=None:False
        changes=[]
        p.set_tun=lambda enabled,cancel=None:changes.append(enabled)
        m._network_tick(clock())
        self.assertEqual([False],changes)
        self.assertGreater(m.state["rest_until"],clock())
