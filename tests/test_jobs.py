import asyncio

from app.jobs import JobManager, ensure_track


class FakeClient:
    def __init__(self, results=None):
        self.results = results or []
        self.searches = []
        self.downloads = []

    async def search(self, *, source, name, pages=1):
        self.searches.append(name)
        return self.results.pop(0) if self.results else []

    async def download(self, payload):
        self.downloads.append(payload)
        return {"status": "success", "filename": "小春 - 小方桌.mp3"}


def test_ensure_track_downloads_best_match():
    client = FakeClient([[
        {"id": "1", "name": "小方桌", "artist": ["小春"], "album": "", "pic_id": "p", "source": "netease"},
        {"id": "2", "name": "小方桌 (Live)", "artist": ["小春"], "album": "", "pic_id": "p", "source": "netease"},
    ]])

    result = asyncio.run(ensure_track(client, {"title": "小方桌", "artist": "小春"}))

    assert result["status"] == "downloaded"
    assert client.downloads[0]["id"] == "1"
    assert client.downloads[0]["artist"] == "小春"


def test_ensure_track_falls_back_to_artist_query():
    client = FakeClient([
        [],
        [{"id": "9", "name": "小方桌", "artist": ["小春"], "album": "", "pic_id": "", "source": "netease"}],
    ])

    result = asyncio.run(ensure_track(client, {"title": "小方桌", "artist": "小春"}))

    assert result["status"] == "downloaded"
    assert client.searches == ["小方桌", "小方桌 小春"]


def test_ensure_track_reports_not_found():
    client = FakeClient([[], []])
    result = asyncio.run(ensure_track(client, {"title": "不存在的歌", "artist": ""}))
    assert result["status"] == "not_found"


def test_job_manager_runs_to_completion(tmp_path):
    client = FakeClient([[
        {"id": "1", "name": "小方桌", "artist": ["小春"], "album": "", "pic_id": "", "source": "netease"},
    ]])

    async def scenario():
        manager = JobManager(client, library_dir=str(tmp_path), match_threshold=0.9)
        await manager.start()
        job = manager.submit(
            [{"title": "小方桌", "artist": "小春"}],
            organize=False,
            scan=False,
        )
        job_id = job["job_id"]
        for _ in range(100):
            await asyncio.sleep(0.02)
            state = manager.get(job_id)
            if state["status"] in {"completed", "failed"}:
                break
        await manager.stop()
        return manager.get(job_id)

    state = asyncio.run(scenario())
    assert state["status"] == "completed"
    assert state["downloaded"] == 1
    assert state["processed"] == 1
