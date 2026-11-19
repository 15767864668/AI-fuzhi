"""Offline regression tests for the standalone infinite canvas HTTP service."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hongguo.canvas import start_canvas_server


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = start_canvas_server(Path(self.temp.name), 0)
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_page_and_roundtrip(self):
        with urlopen(self.base + "/") as res:
            page = res.read().decode("utf-8")
        self.assertIn("无限画布", page)
        self.assertIn("pointerdown", page)
        document = {
            "nodes": [{"id": "a1", "x": 12, "y": 20, "title": "镜头1", "text": "近景"},
                      {"id": "b2", "x": 350, "y": 20, "title": "镜头2", "text": "远景"}],
            "edges": [{"from": "a1", "to": "b2"}],
            "view": {"x": 100, "y": 100, "scale": 1}
        }
        req = Request(self.base + "/api/canvas", data=json.dumps(document).encode(),
                      headers={"Content-Type": "application/json"}, method="PUT")
        with urlopen(req) as res:
            self.assertEqual(res.status, 204)
        with urlopen(self.base + "/api/canvas") as res:
            self.assertEqual(json.load(res), document)

    def test_rejects_bad_edges(self):
        document = {"nodes": [], "edges": [{"from": "missing", "to": "missing"}],
                    "view": {"x": 0, "y": 0, "scale": 1}}
        req = Request(self.base + "/api/canvas", data=json.dumps(document).encode(), method="PUT")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
