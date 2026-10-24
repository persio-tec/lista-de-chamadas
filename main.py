#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MODELS_DIR = APP_DIR / "models"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "lista-de-chamadas"
DATABASE = DATA_DIR / "chamadas.sqlite3"
HOST = "127.0.0.1"
PORT = 8765
# Certificados locais gerados pelo mkcert (grátis, open source).
CERTIFICADOS_DIR = Path.home() / ".config" / "certificados-locais"
ARQUIVO_CERT = CERTIFICADOS_DIR / "localhost.pem"
ARQUIVO_CHAVE = CERTIFICADOS_DIR / "localhost-key.pem"
MATCH_THRESHOLD = 0.363


class AttendanceStore:
    def __init__(self, database=DATABASE):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.database.parent, 0o700)
        except OSError:
            pass
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    face_feature BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attendance (
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    attendance_date TEXT NOT NULL,
                    marked_at TEXT NOT NULL,
                    method TEXT NOT NULL CHECK(method IN ('facial', 'manual')),
                    PRIMARY KEY (student_id, attendance_date)
                );
                CREATE TABLE IF NOT EXISTS student_face_samples (
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    sample_number INTEGER NOT NULL,
                    face_feature BLOB NOT NULL,
                    PRIMARY KEY (student_id, sample_number)
                );
                CREATE TABLE IF NOT EXISTS attendance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    occurred_at TEXT NOT NULL,
                    method TEXT NOT NULL CHECK(method IN ('facial', 'manual'))
                );
                CREATE INDEX IF NOT EXISTS attendance_history_occurred_at
                    ON attendance_history (occurred_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO student_face_samples (student_id, sample_number, face_feature)
                SELECT id, 0, face_feature FROM students;
                """
            )
            migrated = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'attendance_history_v1'"
            ).fetchone()
            if not migrated:
                connection.execute(
                    """
                    INSERT INTO attendance_history (student_id, occurred_at, method)
                    SELECT student_id, marked_at, method FROM attendance
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    (
                        "attendance_history_v1",
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                    ),
                )

        try:
            if self.database.exists():
                os.chmod(self.database, 0o600)
        except OSError:
            pass

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def students(self, attendance_date=None):
        attendance_date = attendance_date or date.today().isoformat()
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.name, a.marked_at, a.method
                FROM students AS s
                LEFT JOIN attendance AS a
                    ON a.student_id = s.id AND a.attendance_date = ?
                ORDER BY s.name COLLATE NOCASE
                """,
                (attendance_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_student(self, name, feature):
        return self.add_student_with_face_samples(name, [feature])

    @staticmethod
    def _validated_face_samples(features):
        vectors = [np.asarray(feature, dtype=np.float32).flatten() for feature in features]
        if not vectors or any(vector.size != 128 or not np.isfinite(vector).all() for vector in vectors):
            raise ValueError("Não foi possível gerar um cadastro facial válido.")
        return vectors

    def add_student_with_face_samples(self, name, features):
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("Informe o nome da pessoa.")
        vectors = self._validated_face_samples(features)
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO students (name, face_feature, created_at) VALUES (?, ?, ?)",
                    (
                        clean_name,
                        vectors[0].tobytes(),
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                    ),
                )
                student_id = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO student_face_samples (student_id, sample_number, face_feature) VALUES (?, ?, ?)",
                    [(student_id, index, vector.tobytes()) for index, vector in enumerate(vectors)],
                )
                return cursor.lastrowid
        except sqlite3.IntegrityError as error:
            if "UNIQUE constraint failed: students.name" in str(error):
                raise ValueError("Já existe uma pessoa cadastrada com esse nome.") from error
            raise

    def update_face_samples(self, student_id, features):
        vectors = self._validated_face_samples(features)
        with self.transaction() as connection:
            student = connection.execute(
                "SELECT id FROM students WHERE id = ?", (student_id,)
            ).fetchone()
            if not student:
                raise ValueError("Pessoa não encontrada. Atualize a lista e tente novamente.")
            connection.execute(
                "DELETE FROM student_face_samples WHERE student_id = ?", (student_id,)
            )
            connection.executemany(
                "INSERT INTO student_face_samples (student_id, sample_number, face_feature) VALUES (?, ?, ?)",
                [(student_id, index, vector.tobytes()) for index, vector in enumerate(vectors)],
            )
            connection.execute(
                "UPDATE students SET face_feature = ? WHERE id = ?",
                (vectors[0].tobytes(), student_id),
            )

    def face_features(self):
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.name, samples.face_feature
                FROM students AS s
                JOIN student_face_samples AS samples ON samples.student_id = s.id
                ORDER BY s.id, samples.sample_number
                """
            ).fetchall()
        return [
            (row["id"], row["name"], np.frombuffer(row["face_feature"], dtype=np.float32).reshape(1, -1).copy())
            for row in rows
        ]

    def mark_present(self, student_id, method):
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO attendance (student_id, attendance_date, marked_at, method)
                VALUES (?, ?, ?, ?)
                """,
                (
                    student_id,
                    date.today().isoformat(),
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    method,
                ),
            )
            return cursor.rowcount == 1

    def record_attendance(self, student_id, method, occurred_at=None):
        if method not in ("facial", "manual"):
            raise ValueError("Tipo de registro inválido.")
        occurred_at = occurred_at or datetime.now().astimezone().isoformat(timespec="seconds")
        attendance_date = date.fromisoformat(occurred_at[:10]).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO attendance_history (student_id, occurred_at, method)
                VALUES (?, ?, ?)
                """,
                (student_id, occurred_at, method),
            )
            connection.execute(
                """
                INSERT INTO attendance (student_id, attendance_date, marked_at, method)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(student_id, attendance_date) DO UPDATE SET
                    marked_at = excluded.marked_at,
                    method = excluded.method
                """,
                (student_id, attendance_date, occurred_at, method),
            )
            return {"id": cursor.lastrowid, "occurred_at": occurred_at}

    def attendance_history(self, attendance_date=None, limit=100, offset=0):
        with self.transaction() as connection:
            where = ""
            parameters = []
            if attendance_date:
                where = "WHERE substr(h.occurred_at, 1, 10) = ?"
                parameters.append(attendance_date)
            total = connection.execute(
                f"SELECT COUNT(*) FROM attendance_history AS h {where}", parameters
            ).fetchone()[0]
            pagination = ""
            if limit is not None:
                pagination = "LIMIT ? OFFSET ?"
                parameters.extend((limit, offset))
            rows = connection.execute(
                f"""
                SELECT h.id, s.id AS student_id, s.name, h.occurred_at, h.method
                FROM attendance_history AS h
                JOIN students AS s ON s.id = h.student_id
                {where}
                ORDER BY h.occurred_at DESC, h.id DESC
                {pagination}
                """,
                parameters,
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": limit is not None and offset + len(rows) < total,
        }

    def export_history_csv(self, attendance_date=None):
        from io import StringIO

        history = self.attendance_history(attendance_date, limit=None)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["Nome", "Data", "Hora", "Tipo de registro"])
        for item in history["items"]:
            writer.writerow(
                [
                    item["name"],
                    item["occurred_at"][:10],
                    item["occurred_at"][11:19],
                    "Reconhecimento facial" if item["method"] == "facial" else "Manual",
                ]
            )
        return output.getvalue()

    def remove_student(self, student_id):
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
            return cursor.rowcount == 1

    def export_csv(self):
        rows = self.students()
        from io import StringIO

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["Nome", "Data", "Presença", "Registro"])
        for student in rows:
            marked_at = student["marked_at"]
            writer.writerow(
                [
                    student["name"],
                    date.today().isoformat(),
                    "Presente" if marked_at else "Ausente",
                    student["method"] or "",
                ]
            )
        return output.getvalue()


class CameraService:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.model_lock = threading.RLock()
        self.capture = None
        self.camera_device = None
        self.worker = None
        self.running = False
        self.latest_frame = None
        self.latest_frame_at = None
        self.latest_jpeg = None
        self.latest_error = None
        self.last_match = None
        self.latest_attendance_event = None
        self.recognition_status = None
        self.visible_students = set()
        self.missed_face_frames = {}
        self.visible_student_events = {}
        self.detector = None
        self.recognizer = None
        self.model_error = None
        self._load_models()

    def _load_models(self):
        detector_path = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
        recognizer_path = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
        if not detector_path.is_file() or not recognizer_path.is_file():
            self.model_error = "Os modelos faciais não foram instalados junto com o aplicativo."
            return
        try:
            self.detector = cv2.FaceDetectorYN_create(
                str(detector_path), "", (320, 320), 0.8, 0.3, 5000
            )
            self.recognizer = cv2.FaceRecognizerSF_create(str(recognizer_path), "")
        except cv2.error as error:
            self.model_error = f"Não foi possível carregar os modelos faciais: {error}"

    def start(self):
        with self.lock:
            if self.model_error:
                raise RuntimeError(self.model_error)
            if self.running:
                return
            devices = sorted(
                Path("/dev").glob("video[0-9]*"),
                key=lambda path: int(path.name.removeprefix("video")),
            )
            if not devices:
                devices = [None]

            capture = None
            camera_device = None
            opened_without_frames = False
            for device in devices:
                source = str(device) if device else 0
                candidate = cv2.VideoCapture(source, cv2.CAP_V4L2)
                if not candidate.isOpened():
                    candidate.release()
                    continue
                ok, frame = candidate.read()
                if ok and frame is not None:
                    capture = candidate
                    camera_device = source
                    self.latest_frame = frame
                    self.latest_frame_at = time.monotonic()
                    break
                opened_without_frames = True
                candidate.release()
            if capture is None:
                if opened_without_frames:
                    raise RuntimeError(
                        "A câmera foi encontrada, mas não enviou imagem. Feche outros aplicativos "
                        "que possam estar usando a câmera e tente novamente."
                    )
                raise RuntimeError(
                    "Nenhuma câmera de vídeo foi encontrada. Verifique se ela está conectada "
                    "e se o usuário tem permissão para acessá-la."
                )
            self.capture = capture
            self.camera_device = camera_device
            self.latest_error = None
            self.running = True
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()

    def stop(self):
        with self.lock:
            self.running = False
            worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=3)
        with self.lock:
            if self.capture:
                self.capture.release()
            self.capture = None
            self.camera_device = None
            self.worker = None
            self.latest_frame = None
            self.latest_frame_at = None
            self.latest_jpeg = None
            self.visible_students.clear()
            self.missed_face_frames.clear()
            self.visible_student_events.clear()

    def _run(self):
        while True:
            with self.lock:
                if not self.running:
                    return
                capture = self.capture
            ok, frame = capture.read()
            if not ok:
                with self.lock:
                    self.latest_error = "A câmera parou de enviar imagem."
                time.sleep(0.1)
                continue
            with self.lock:
                self.latest_frame = frame.copy()
                self.latest_frame_at = time.monotonic()
            if frame.shape[1] > 960:
                frame = cv2.resize(frame, (960, int(frame.shape[0] * 960 / frame.shape[1])))
            preview = self._process_frame(frame)
            encoded_ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 78])
            if encoded_ok:
                with self.lock:
                    self.latest_frame = frame.copy()
                    self.latest_jpeg = encoded.tobytes()
                    self.latest_error = None

    def _detect(self, frame):
        with self.model_lock:
            self.detector.setInputSize((frame.shape[1], frame.shape[0]))
            _, faces = self.detector.detect(frame)
            return faces

    def _feature(self, frame, face):
        with self.model_lock:
            aligned = self.recognizer.alignCrop(frame, face)
            return self.recognizer.feature(aligned).astype(np.float32).reshape(1, -1)

    def _process_frame(self, frame):
        preview = frame.copy()
        try:
            faces = self._detect(frame)
            if faces is None or len(faces) == 0:
                self._advance_recognition(None)
                with self.lock:
                    self.last_match = None
                    self.recognition_status = None
                return preview
            if len(faces) != 1:
                self._advance_recognition(None)
                for face in faces:
                    x, y, width, height = face[:4].astype(int)
                    cv2.rectangle(preview, (x, y), (x + width, y + height), (30, 160, 245), 2)
                cv2.putText(
                    preview, "Uma pessoa por vez", (18, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 160, 245), 2,
                )
                with self.lock:
                    self.last_match = None
                    self.recognition_status = {"status": "multiple_faces"}
                return preview

            feature = self._feature(frame, faces[0])
            best_student = None
            best_score = -1.0
            for student_id, name, known_feature in self.store.face_features():
                with self.model_lock:
                    score = float(
                        self.recognizer.match(
                            feature, known_feature, cv2.FaceRecognizerSF_FR_COSINE
                        )
                    )
                if score > best_score:
                    best_student = (student_id, name)
                    best_score = score

            label = "Rosto nao cadastrado"
            color = (30, 160, 245)
            if best_student and best_score >= MATCH_THRESHOLD:
                student_id, name = best_student
                was_visible, event = self._advance_recognition((student_id, name))
                label = f"{name} - registrado" if not was_visible else f"{name} - reconhecido"
                color = (55, 190, 120)
                with self.lock:
                    self.last_match = {
                        "name": name,
                        "score": round(best_score, 3),
                        "id": event["id"] if event else None,
                        "occurred_at": event["occurred_at"] if event else None,
                        "new_passage": not was_visible,
                    }
                    self.recognition_status = {
                        "status": "recognized",
                        "score": round(best_score, 3),
                        "new_passage": not was_visible,
                    }
            else:
                self._advance_recognition(None)
                with self.lock:
                    self.last_match = None
                    self.recognition_status = {
                        "status": "unrecognized",
                        "score": round(best_score, 3) if best_student else None,
                    }
            x, y, width, height = faces[0][:4].astype(int)
            cv2.rectangle(preview, (x, y), (x + width, y + height), color, 2)
            cv2.putText(
                preview, label, (max(12, x), max(26, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2,
            )
        except cv2.error as error:
            with self.lock:
                self.latest_error = f"Falha no reconhecimento facial: {error}"
        return preview

    def _advance_recognition(self, recognized_student):
        recognized_id = recognized_student[0] if recognized_student else None
        for student_id in tuple(self.visible_students):
            if student_id == recognized_id:
                continue
            missed_frames = self.missed_face_frames.get(student_id, 0) + 1
            self.missed_face_frames[student_id] = missed_frames
            if missed_frames >= 3:
                self.visible_students.remove(student_id)
                self.missed_face_frames.pop(student_id, None)
                self.visible_student_events.pop(student_id, None)

        if recognized_student is not None:
            student_id, name = recognized_student
            self.missed_face_frames[student_id] = 0
            if student_id in self.visible_students:
                return True, self.visible_student_events[student_id]
            attendance = self.store.record_attendance(student_id, "facial")
            self.visible_students.add(student_id)
            self.visible_student_events[student_id] = attendance
            with self.lock:
                self.latest_attendance_event = attendance
                self.last_match = {
                    "name": name,
                    "score": None,
                    "id": attendance["id"] if attendance else None,
                    "occurred_at": attendance["occurred_at"] if attendance else None,
                    "new_passage": True,
                }
            return False, attendance
        return False, None

    def _capture_face_samples(self, count=5):
        with self.lock:
            if not self.running:
                raise RuntimeError("Ative a chamada automática antes de atualizar o rosto.")
            previous_frame_at = self.latest_frame_at - 0.001 if self.latest_frame_at else 0
        samples = []
        deadline = time.monotonic() + 25
        saw_multiple_faces = False
        while len(samples) < count and time.monotonic() < deadline:
            with self.lock:
                frame = (
                    self.latest_frame.copy()
                    if self.latest_frame is not None
                    and self.latest_frame_at is not None
                    and self.latest_frame_at > previous_frame_at
                    else None
                )
                if frame is not None:
                    previous_frame_at = self.latest_frame_at
            if frame is None:
                time.sleep(0.03)
                continue

            faces = self._detect(frame)
            if faces is None or len(faces) == 0:
                time.sleep(0.03)
                continue
            if len(faces) != 1:
                saw_multiple_faces = True
                time.sleep(0.03)
                continue
            if faces[0][2] < 60 or faces[0][3] < 60:
                raise ValueError("Aproxime o rosto da câmera para atualizar o cadastro.")
            feature = self._feature(frame, faces[0])
            if samples:
                with self.model_lock:
                    same_person_score = float(
                        self.recognizer.match(
                            samples[0], feature, cv2.FaceRecognizerSF_FR_COSINE
                        )
                    )
                if same_person_score < MATCH_THRESHOLD:
                    raise ValueError(
                        "O rosto mudou durante a captura. Mantenha a mesma pessoa em frente à câmera."
                    )
            samples.append(feature)
            if len(samples) < count:
                time.sleep(0.12)
        if len(samples) != count:
            if saw_multiple_faces:
                raise ValueError(
                    "Deixe apenas uma pessoa visível e tente atualizar o cadastro novamente."
                )
            raise RuntimeError("A câmera não enviou imagens suficientes. Confira a câmera e tente novamente.")
        return samples

    def enroll_current_face(self, name, consent):
        if consent is not True:
            raise ValueError("Confirme que a pessoa consentiu com o cadastro facial.")
        samples = self._capture_face_samples()
        return self.store.add_student_with_face_samples(name, samples)

    def update_student_face(self, student_id, consent):
        if consent is not True:
            raise ValueError("Confirme que a pessoa consentiu com a atualização do cadastro facial.")
        samples = self._capture_face_samples()
        self.store.update_face_samples(student_id, samples)
        with self.lock:
            self.last_match = None
            self.recognition_status = None
        return len(samples)

    def status(self):
        with self.lock:
            return {
                "camera_running": self.running,
                "camera_device": self.camera_device,
                "camera_error": self.latest_error,
                "model_error": self.model_error,
                "last_match": self.last_match,
                "latest_attendance_event": self.latest_attendance_event,
                "recognition_status": self.recognition_status,
                "students": self.store.students(),
                "date": date.today().isoformat(),
            }

    def video_stream(self):
        while True:
            with self.lock:
                if not self.running:
                    return
                jpeg = self.latest_jpeg
            if jpeg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-cache\r\n\r\n" + jpeg + b"\r\n"
            time.sleep(0.04)


class AppHandler(BaseHTTPRequestHandler):
    store = None
    camera = None

    def log_message(self, format_string, *args):
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def handle_one_request(self):
        self.server.last_request = time.monotonic()
        super().handle_one_request()

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False))

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError("A solicitação está vazia ou excede o limite permitido.")
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Os dados enviados não são um JSON válido.") from error

    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/":
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._serve_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._serve_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
        elif path == "/icon.svg":
            self._serve_file(APP_DIR / "icon.svg", "image/svg+xml")
        elif path == "/api/status":
            self._json(200, self.camera.status())
        elif path == "/api/export.csv":
            payload = b"\xef\xbb\xbf" + self.store.export_csv().encode("utf-8")
            self._send(200, payload, "text/csv; charset=utf-8")
        elif path == "/api/history":
            try:
                attendance_date, limit, offset = self._history_parameters(query)
                self._json(
                    200,
                    self.store.attendance_history(attendance_date, limit, offset),
                )
            except ValueError as error:
                self._json(400, {"error": str(error)})
        elif path == "/api/history.csv":
            try:
                attendance_date, _, _ = self._history_parameters(query)
                payload = b"\xef\xbb\xbf" + self.store.export_history_csv(
                    attendance_date
                ).encode("utf-8")
                self._send(200, payload, "text/csv; charset=utf-8")
            except ValueError as error:
                self._json(400, {"error": str(error)})
        elif path == "/video":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for chunk in self.camera.video_stream():
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._json(404, {"error": "Página não encontrada."})

    @staticmethod
    def _history_parameters(query):
        raw_date = query.get("date", [""])[0]
        if raw_date:
            try:
                attendance_date = date.fromisoformat(raw_date).isoformat()
            except ValueError as error:
                raise ValueError("Data inválida. Use o formato AAAA-MM-DD.") from error
        else:
            attendance_date = None
        try:
            limit = int(query.get("limit", ["100"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError as error:
            raise ValueError("Paginação do histórico inválida.") from error
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Limite ou início da página fora do intervalo permitido.")
        return attendance_date, limit, offset

    def _serve_file(self, path, content_type):
        try:
            self._send(200, path.read_bytes(), content_type)
        except OSError:
            self._json(500, {"error": "Não foi possível carregar os arquivos do aplicativo."})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/camera/start":
                self.camera.start()
                self._json(200, {"ok": True})
            elif path == "/api/camera/stop":
                self.camera.stop()
                self._json(200, {"ok": True})
            elif path == "/api/enroll":
                body = self._read_json()
                student_id = self.camera.enroll_current_face(body.get("name", ""), body.get("consent"))
                self._json(201, {"ok": True, "student_id": student_id})
            elif path == "/api/attendance/manual":
                body = self._read_json()
                student_id = int(body.get("student_id", 0))
                attendance = self.store.record_attendance(student_id, "manual")
                self._json(201, {"ok": True, **attendance})
            else:
                self._json(404, {"error": "Ação não encontrada."})
        except (ValueError, RuntimeError, sqlite3.IntegrityError) as error:
            self._json(400, {"error": str(error)})
        except (cv2.error, OSError) as error:
            self._json(500, {"error": f"Falha no aplicativo: {error}"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/students/"):
            self._json(404, {"error": "Ação não encontrada."})
            return
        try:
            student_id = int(path.removeprefix("/api/students/"))
        except ValueError:
            self._json(400, {"error": "Identificador inválido."})
            return
        if self.store.remove_student(student_id):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "Pessoa não encontrada."})

    def do_PUT(self):
        path = urlparse(self.path).path
        prefix = "/api/students/"
        suffix = "/face"
        if not path.startswith(prefix) or not path.endswith(suffix):
            self._json(404, {"error": "Ação não encontrada."})
            return
        try:
            student_id = int(path[len(prefix):-len(suffix)])
            body = self._read_json()
            sample_count = self.camera.update_student_face(student_id, body.get("consent"))
            self._json(200, {"ok": True, "samples": sample_count})
        except (ValueError, RuntimeError) as error:
            self._json(400, {"error": str(error)})
        except (cv2.error, OSError, sqlite3.Error) as error:
            self._json(500, {"error": f"Falha ao atualizar o rosto: {error}"})


def contexto_https():
    """Monta o contexto TLS com os certificados locais do mkcert.

    Retorna ``None`` quando os certificados ainda não foram gerados, para que
    o servidor siga em HTTP sem quebrar nada.
    """
    if not (ARQUIVO_CERT.exists() and ARQUIVO_CHAVE.exists()):
        return None
    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    contexto.load_cert_chain(str(ARQUIVO_CERT), str(ARQUIVO_CHAVE))
    return contexto


def serve(https=False):
    if not hasattr(cv2, "FaceDetectorYN_create") or not hasattr(cv2, "FaceRecognizerSF_create"):
        raise RuntimeError("Este aplicativo precisa do OpenCV com suporte aos modelos faciais YuNet e SFace.")
    AppHandler.store = AttendanceStore()
    AppHandler.camera = CameraService(AppHandler.store)
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    server.daemon_threads = True
    esquema = "http"
    if https:
        contexto = contexto_https()
        if contexto is None:
            raise RuntimeError(
                "Certificados locais não encontrados em "
                f"{CERTIFICADOS_DIR}. Gere com: mkcert -install && "
                f"mkcert -cert-file {ARQUIVO_CERT} -key-file {ARQUIVO_CHAVE} "
                f"localhost 127.0.0.1"
            )
        server.socket = contexto.wrap_socket(server.socket, server_side=True)
        esquema = "https"
    print(f"Lista de Chamadas em {esquema}://{HOST}:{PORT}")
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server_thread.join(timeout=3)
        AppHandler.camera.stop()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor local da Lista de Chamadas")
    parser.add_argument("--serve", action="store_true", help="Inicia a aplicação local")
    args = parser.parse_args()
    if args.serve:
        serve()
    else:
        parser.print_help()
