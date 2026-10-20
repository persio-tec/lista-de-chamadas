import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

import cv2
import numpy as np

from main import AppHandler, AttendanceStore, CameraService
from http.server import ThreadingHTTPServer


class AttendanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AttendanceStore(Path(self.temp_dir.name) / "attendance.sqlite3")
        self.feature = np.zeros(128, dtype=np.float32)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_student_and_round_trip_face_feature(self):
        student_id = self.store.add_student("  Maria   Silva  ", self.feature)

        self.assertEqual(self.store.students()[0]["name"], "Maria Silva")
        saved_id, saved_name, saved_feature = self.store.face_features()[0]
        self.assertEqual((saved_id, saved_name), (student_id, "Maria Silva"))
        np.testing.assert_array_equal(saved_feature, self.feature.reshape(1, -1))

    def test_add_student_keeps_multiple_face_samples(self):
        samples = [np.full(128, value, dtype=np.float32) for value in (0.1, 0.2, 0.3)]
        student_id = self.store.add_student_with_face_samples("Maria Silva", samples)

        saved = self.store.face_features()
        self.assertEqual([row[:2] for row in saved], [(student_id, "Maria Silva")] * 3)
        for result, sample in zip(saved, samples):
            np.testing.assert_array_equal(result[2], sample.reshape(1, -1))

    def test_updating_face_samples_preserves_attendance(self):
        student_id = self.store.add_student("Maria Silva", self.feature)
        self.store.mark_present(student_id, "manual")
        samples = [np.full(128, value, dtype=np.float32) for value in (0.4, 0.5)]

        self.store.update_face_samples(student_id, samples)

        saved = self.store.face_features()
        self.assertEqual(len(saved), 2)
        for result, sample in zip(saved, samples):
            np.testing.assert_array_equal(result[2], sample.reshape(1, -1))
        self.assertEqual(self.store.students()[0]["method"], "manual")

    def test_existing_single_face_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE students (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        face_feature BLOB NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO students VALUES (1, 'Maria Silva', ?, '2026-01-01')",
                    (self.feature.tobytes(),),
                )

            migrated_store = AttendanceStore(database)

            self.assertEqual(len(migrated_store.face_features()), 1)
            np.testing.assert_array_equal(
                migrated_store.face_features()[0][2],
                self.feature.reshape(1, -1),
            )

    def test_duplicate_name_is_reported_clearly(self):
        self.store.add_student("Maria Silva", self.feature)

        with self.assertRaisesRegex(ValueError, "Já existe uma pessoa"):
            self.store.add_student("maria silva", self.feature)

    def test_presence_can_be_marked_only_once_per_day(self):
        student_id = self.store.add_student("Maria Silva", self.feature)

        self.assertTrue(self.store.mark_present(student_id, "facial"))
        self.assertFalse(self.store.mark_present(student_id, "manual"))
        self.assertEqual(self.store.students()[0]["method"], "facial")

    def test_history_records_each_passage_and_updates_daily_summary(self):
        student_id = self.store.add_student("Maria Silva", self.feature)
        for minute in range(10):
            self.store.record_attendance(
                student_id,
                "facial",
                f"2026-04-10T09:{minute:02d}:00+00:00",
            )

        history = self.store.attendance_history("2026-04-10", limit=20)
        self.assertEqual(history["total"], 10)
        self.assertEqual(len(history["items"]), 10)
        self.assertEqual(history["items"][0]["occurred_at"], "2026-04-10T09:09:00+00:00")
        self.assertEqual(self.store.students("2026-04-10")[0]["marked_at"], "2026-04-10T09:09:00+00:00")

    def test_history_filter_pagination_and_csv_export(self):
        student_id = self.store.add_student("Maria Silva", self.feature)
        self.store.record_attendance(student_id, "manual", "2026-04-10T09:00:00+00:00")
        self.store.record_attendance(student_id, "facial", "2026-04-11T10:30:00+00:00")

        page = self.store.attendance_history("2026-04-11", limit=1, offset=0)
        self.assertEqual(page["total"], 1)
        self.assertFalse(page["has_more"])
        self.assertEqual(page["items"][0]["method"], "facial")
        self.assertIn("Maria Silva,2026-04-10,09:00:00,Manual", self.store.export_history_csv("2026-04-10"))

    def test_legacy_daily_attendance_is_migrated_to_history_only_once(self):
        database = Path(self.temp_dir.name) / "legacy-attendance.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE students (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    face_feature BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE attendance (
                    student_id INTEGER NOT NULL,
                    attendance_date TEXT NOT NULL,
                    marked_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    PRIMARY KEY (student_id, attendance_date)
                );
                """
            )
            connection.execute(
                "INSERT INTO students VALUES (1, 'Maria Silva', ?, '2026-01-01')",
                (self.feature.tobytes(),),
            )
            connection.execute(
                "INSERT INTO attendance VALUES (1, '2026-04-10', '2026-04-10T09:00:00+00:00', 'facial')"
            )

        migrated_store = AttendanceStore(database)
        AttendanceStore(database)

        history = migrated_store.attendance_history()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["method"], "facial")

    def test_removing_student_also_removes_attendance(self):
        student_id = self.store.add_student("Maria Silva", self.feature)
        self.store.mark_present(student_id, "manual")
        self.store.record_attendance(student_id, "manual")

        self.assertTrue(self.store.remove_student(student_id))
        self.assertEqual(self.store.students(), [])
        self.assertEqual(self.store.face_features(), [])
        self.assertEqual(self.store.attendance_history()["total"], 0)


class CameraServiceTests(unittest.TestCase):
    def test_camera_uses_v4l2_and_keeps_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AttendanceStore(Path(directory) / "attendance.sqlite3")
            camera = CameraService(store)
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            capture = Mock()
            capture.isOpened.return_value = True
            capture.read.return_value = (True, frame)

            with (
                patch("main.Path.glob", return_value=[Path("/dev/video0")]),
                patch("main.cv2.VideoCapture", return_value=capture) as open_camera,
                patch.object(camera, "_run"),
            ):
                camera.start()
                self.assertEqual(open_camera.call_args.args, ("/dev/video0", cv2.CAP_V4L2))
                self.assertEqual(camera.camera_device, "/dev/video0")
                np.testing.assert_array_equal(camera.latest_frame, frame)
                camera.stop()

    def test_recognized_face_is_marked_automatically(self):
        store = Mock()
        feature = np.ones((1, 128), dtype=np.float32)
        store.face_features.return_value = [(7, "Maria Silva", feature)]
        faces = np.zeros((1, 15), dtype=np.float32)
        faces[0, :4] = (10, 20, 80, 90)

        with patch("main.CameraService._load_models"):
            camera = CameraService(store)
        camera.detector = Mock()
        camera.detector.detect.return_value = (None, faces)
        camera.recognizer = Mock()
        camera.recognizer.match.return_value = 0.9
        camera.recognizer.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
        camera.recognizer.feature.return_value = feature
        store.record_attendance.return_value = {
            "id": 1,
            "occurred_at": "2026-04-10T09:00:00+00:00",
        }

        camera._process_frame(np.zeros((160, 160, 3), dtype=np.uint8))
        camera._process_frame(np.zeros((160, 160, 3), dtype=np.uint8))

        store.record_attendance.assert_called_once_with(7, "facial")
        self.assertEqual(camera.last_match, {
            "name": "Maria Silva",
            "score": 0.9,
            "id": 1,
            "occurred_at": "2026-04-10T09:00:00+00:00",
            "new_passage": False,
        })
        self.assertEqual(camera.recognition_status, {
            "status": "recognized",
            "score": 0.9,
            "new_passage": False,
        })

    def test_recognition_uses_best_matching_face_sample(self):
        store = Mock()
        feature = np.ones((1, 128), dtype=np.float32)
        store.face_features.return_value = [
            (7, "Maria Silva", feature),
            (7, "Maria Silva", feature),
        ]
        faces = np.zeros((1, 15), dtype=np.float32)
        faces[0, :4] = (10, 20, 80, 90)

        with patch("main.CameraService._load_models"):
            camera = CameraService(store)
        camera.detector = Mock()
        camera.detector.detect.return_value = (None, faces)
        camera.recognizer = Mock()
        camera.recognizer.match.side_effect = (0.15, 0.72)
        camera.recognizer.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
        camera.recognizer.feature.return_value = feature
        store.record_attendance.return_value = {
            "id": 1,
            "occurred_at": "2026-04-10T09:00:00+00:00",
        }

        camera._process_frame(np.zeros((160, 160, 3), dtype=np.uint8))

        store.record_attendance.assert_called_once_with(7, "facial")
        self.assertEqual(camera.last_match["score"], 0.72)
        self.assertTrue(camera.last_match["new_passage"])

    def test_unrecognized_face_reports_low_matching_score(self):
        store = Mock()
        feature = np.ones((1, 128), dtype=np.float32)
        store.face_features.return_value = [(7, "Maria Silva", feature)]
        faces = np.zeros((1, 15), dtype=np.float32)
        faces[0, :4] = (10, 20, 80, 90)

        with patch("main.CameraService._load_models"):
            camera = CameraService(store)
        camera.detector = Mock()
        camera.detector.detect.return_value = (None, faces)
        camera.recognizer = Mock()
        camera.recognizer.match.return_value = 0.15
        camera.recognizer.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
        camera.recognizer.feature.return_value = feature

        camera._process_frame(np.zeros((160, 160, 3), dtype=np.uint8))

        store.record_attendance.assert_not_called()
        self.assertEqual(camera.recognition_status, {"status": "unrecognized", "score": 0.15})

    def test_each_separate_camera_appearance_creates_a_history_event(self):
        store = Mock()
        store.record_attendance.side_effect = [
            {"id": event_id, "occurred_at": f"2026-04-10T09:{event_id:02d}:00+00:00"}
            for event_id in range(1, 11)
        ]
        with patch("main.CameraService._load_models"):
            camera = CameraService(store)

        for _ in range(10):
            camera._advance_recognition((7, "Maria Silva"))
            for _ in range(20):
                camera._advance_recognition((7, "Maria Silva"))
            self.assertTrue(camera.visible_students)
            for _ in range(3):
                camera._advance_recognition(None)

        self.assertEqual(store.record_attendance.call_count, 10)

    def test_recognizing_another_person_rearms_previous_person_after_three_frames(self):
        store = Mock()
        store.record_attendance.side_effect = [
            {"id": event_id, "occurred_at": f"2026-04-10T09:{event_id:02d}:00+00:00"}
            for event_id in range(1, 4)
        ]
        with patch("main.CameraService._load_models"):
            camera = CameraService(store)

        camera._advance_recognition((7, "Maria Silva"))
        for _ in range(3):
            camera._advance_recognition((8, "Joao Silva"))
        camera._advance_recognition((7, "Maria Silva"))

        self.assertEqual(store.record_attendance.call_args_list, [
            unittest.mock.call(7, "facial"),
            unittest.mock.call(8, "facial"),
            unittest.mock.call(7, "facial"),
        ])

    def test_face_refresh_requires_consent_and_replaces_stored_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AttendanceStore(Path(directory) / "attendance.sqlite3")
            student_id = store.add_student("Maria Silva", np.zeros(128, dtype=np.float32))
            camera = CameraService(store)
            refreshed_samples = [
                np.full((1, 128), value, dtype=np.float32) for value in (0.4, 0.5, 0.6)
            ]
            with patch.object(camera, "_capture_face_samples", return_value=refreshed_samples):
                with self.assertRaisesRegex(ValueError, "consentiu"):
                    camera.update_student_face(student_id, False)

                self.assertEqual(camera.update_student_face(student_id, True), 3)

            self.assertEqual(len(store.face_features()), 3)
            for result, sample in zip(store.face_features(), refreshed_samples):
                np.testing.assert_array_equal(result[2], sample)


class AttendanceHistoryAPITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AttendanceStore(Path(self.temp_dir.name) / "attendance.sqlite3")
        self.student_id = self.store.add_student(
            "Maria Silva", np.zeros(128, dtype=np.float32)
        )
        self.store.record_attendance(
            self.student_id, "facial", "2026-04-10T09:00:00+00:00"
        )
        self.store.record_attendance(
            self.student_id, "manual", "2026-04-10T10:00:00+00:00"
        )
        self.previous_store = AppHandler.store
        self.previous_camera = AppHandler.camera
        AppHandler.store = self.store
        AppHandler.camera = Mock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        AppHandler.store = self.previous_store
        AppHandler.camera = self.previous_camera
        self.temp_dir.cleanup()

    def test_history_endpoint_filters_and_paginates_events(self):
        with urlopen(
            f"{self.base_url}/api/history?date=2026-04-10&limit=1&offset=0"
        ) as response:
            result = json.loads(response.read())

        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["has_more"])

    def test_history_endpoint_rejects_invalid_date(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/history?date=not-a-date")

        self.assertEqual(error.exception.code, 400)

    def test_manual_endpoint_creates_history_event_and_csv_exports_it(self):
        request = Request(
            f"{self.base_url}/api/attendance/manual",
            data=json.dumps({"student_id": self.student_id}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)

        with urlopen(f"{self.base_url}/api/history?limit=10") as response:
            result = json.loads(response.read())
        with urlopen(f"{self.base_url}/api/history.csv?date=2026-04-10") as response:
            export = response.read().decode("utf-8-sig")

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["items"][0]["method"], "manual")
        self.assertIn("Maria Silva", export)
        self.assertIn("Manual", export)


if __name__ == "__main__":
    unittest.main()
