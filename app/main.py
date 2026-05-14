import asyncio
import os
import json
import re
import threading
import duckdb
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
import yt_dlp
from sentence_transformers import SentenceTransformer
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lsa import LsaSummarizer
from rrf import rrf_fuse

app = FastAPI(title="YouTube Transcript Library MCP v2")

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "transcripts.db")
os.makedirs(DATA_DIR, exist_ok=True)

con = duckdb.connect(DB_PATH)
_db_lock = threading.Lock()
con.execute("""
CREATE TABLE IF NOT EXISTS transcripts (
    video_id   TEXT PRIMARY KEY,
    title      TEXT,
    channel    TEXT,
    published  TEXT,
    duration   INTEGER,
    language   TEXT,
    transcript TEXT,
    summary    TEXT,
    chapters   JSON,
    embedding  FLOAT[384],
    raw        JSON,
    thumbnail  TEXT,
    category   TEXT DEFAULT 'Uncategorized',
    fetched_at TIMESTAMP
)
""")

# Guarded migration: add `category` to DBs created before this column existed.
_existing_cols = {row[1] for row in con.execute("PRAGMA table_info('transcripts')").fetchall()}
if "category" not in _existing_cols:
    con.execute("ALTER TABLE transcripts ADD COLUMN category TEXT DEFAULT 'Uncategorized'")

embedder = SentenceTransformer("all-MiniLM-L6-v2")
ytt_api = YouTubeTranscriptApi()

_UI_HTML = open(os.path.join(os.path.dirname(__file__), "index.html")).read()


class TranscriptRequest(BaseModel):
    video_id_or_url: str
    lang: Optional[str] = None
    include_timestamps: bool = True


class CategoryUpdate(BaseModel):
    category: str


def parse_video_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    if re.search(r"(youtube\.com|youtu\.be)", url_or_id):
        match = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", url_or_id)
        return match.group(1) if match else url_or_id
    return url_or_id


def get_metadata(video_id: str) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return {
            "title": info.get("title"),
            "channel": info.get("channel"),
            "published": info.get("upload_date"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "chapters": info.get("chapters") or [],
        }


def pick_transcript(transcript_list, lang: Optional[str]):
    if lang:
        return transcript_list.find_transcript([lang])
    try:
        return transcript_list.find_transcript(["en"])
    except NoTranscriptFound:
        return next(iter(transcript_list))


def format_transcript(raw_data: list, include_timestamps: bool) -> str:
    if include_timestamps:
        return "\n".join(f"[{int(e['start'])}s] {e['text']}" for e in raw_data)
    return " ".join(e["text"] for e in raw_data)


def summarize_text(text: str, sentence_count: int = 8) -> str:
    try:
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summary = LsaSummarizer()(parser.document, sentence_count)
        return " ".join(str(s) for s in summary)
    except Exception:
        sentences = text.split(". ")
        return ". ".join(sentences[:4] + sentences[-2:]) + "."


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def ui():
    return _UI_HTML


@app.get("/ui/fetch")
async def ui_fetch_stream(
    url: str = Query(...),
    lang: Optional[str] = Query(None),
    timestamps: bool = Query(True),
):
    """SSE endpoint: streams progress steps then sends the completed result."""

    async def stream():
        def evt(step: str, msg: str, **kw) -> str:
            return f"data: {json.dumps({'step': step, 'msg': msg, **kw})}\n\n"

        video_id = parse_video_id(url)
        yield evt("info", f"Video ID: {video_id}")

        yield evt("checking", "Checking library cache…")
        with _db_lock:
            row = con.execute("""
                SELECT video_id, title, channel, language, summary, chapters, raw, fetched_at, thumbnail
                FROM transcripts WHERE video_id = ?
            """, (video_id,)).fetchone()

        if row:
            vid_id, title, channel, language, summary, chapters_json, raw_json, fetched_at, thumbnail = row
            raw_data = json.loads(raw_json) if raw_json else []
            yield evt("done", f"Loaded from cache: {title}", result={
                "video_id": vid_id, "title": title, "channel": channel,
                "language": language,
                "transcript": format_transcript(raw_data, timestamps),
                "summary": summary,
                "chapters": json.loads(chapters_json) if chapters_json else [],
                "fetched_at": str(fetched_at),
                "thumbnail": thumbnail,
                "cached": True,
            })
            return

        yield evt("metadata", "Fetching video metadata via yt-dlp…")
        try:
            meta = await asyncio.to_thread(get_metadata, video_id)
        except Exception as e:
            yield evt("error", f"Metadata fetch failed: {e}")
            return
        yield evt("metadata_ok", f'Got: "{meta["title"]}"')

        yield evt("transcript", "Downloading transcript…")
        try:
            transcript_list = await asyncio.to_thread(ytt_api.list, video_id)
            transcript_obj = pick_transcript(transcript_list, lang)
            fetched_raw = await asyncio.to_thread(transcript_obj.fetch)
            raw_data = [
                {"text": e.text, "start": e.start, "duration": e.duration}
                for e in fetched_raw
            ]
        except (TranscriptsDisabled, NoTranscriptFound):
            yield evt("error", "No transcripts available for this video.")
            return
        except Exception as e:
            yield evt("error", f"Transcript fetch failed: {e}")
            return
        yield evt("transcript_ok", f"Got {len(raw_data)} segments.")

        text = format_transcript(raw_data, timestamps)
        clean_text = re.sub(r"\[\d+s\] ", "", text)

        yield evt("summarizing", "Generating extractive summary…")
        summary = await asyncio.to_thread(summarize_text, clean_text, 10)

        yield evt("embedding", "Computing semantic embedding…")
        embedding = await asyncio.to_thread(lambda: embedder.encode(summary).tolist())

        yield evt("saving", "Saving to library…")
        with _db_lock:
            con.execute("""
                INSERT INTO transcripts
                    (video_id, title, channel, published, duration, language,
                     transcript, summary, chapters, embedding, raw, thumbnail, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                video_id,
                meta["title"] or "Unknown Title",
                meta["channel"] or "Unknown Channel",
                meta["published"], meta["duration"],
                transcript_obj.language_code,
                text, summary,
                json.dumps(meta["chapters"]),
                embedding, json.dumps(raw_data),
                meta["thumbnail"], datetime.now(),
            ))

        yield evt("done", "Saved to library.", result={
            "video_id": video_id,
            "title": meta["title"] or "Unknown Title",
            "channel": meta["channel"] or "Unknown Channel",
            "language": transcript_obj.language_code,
            "transcript": text,
            "summary": summary,
            "chapters": meta["chapters"],
            "fetched_at": str(datetime.now()),
            "thumbnail": meta["thumbnail"],
            "cached": False,
        })

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    with _db_lock:
        count = con.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    return {"status": "ok", "library_size": count, "db": DB_PATH}


@app.post("/get_transcript")
def get_transcript(req: TranscriptRequest):
    video_id = parse_video_id(req.video_id_or_url)

    with _db_lock:
        row = con.execute("""
            SELECT video_id, title, channel, language, summary, chapters, raw, fetched_at, thumbnail
            FROM transcripts WHERE video_id = ?
        """, (video_id,)).fetchone()

    if row:
        vid_id, title, channel, language, summary, chapters_json, raw_json, fetched_at, thumbnail = row
        raw_data = json.loads(raw_json) if raw_json else []
        return {
            "video_id": vid_id, "title": title, "channel": channel, "language": language,
            "transcript": format_transcript(raw_data, req.include_timestamps),
            "summary": summary,
            "chapters": json.loads(chapters_json) if chapters_json else [],
            "fetched_at": str(fetched_at),
            "thumbnail": thumbnail,
            "cached": True,
        }

    try:
        meta = get_metadata(video_id)
        transcript_list = ytt_api.list(video_id)
        transcript_obj = pick_transcript(transcript_list, req.lang)
        fetched_raw = transcript_obj.fetch()
        raw_data = [
            {"text": e.text, "start": e.start, "duration": e.duration}
            for e in fetched_raw
        ]
        text = format_transcript(raw_data, req.include_timestamps)
        clean_text = re.sub(r"\[\d+s\] ", "", text)
        summary = summarize_text(clean_text, 10)
        embedding = embedder.encode(summary).tolist()

        with _db_lock:
            con.execute("""
                INSERT INTO transcripts
                    (video_id, title, channel, published, duration, language,
                     transcript, summary, chapters, embedding, raw, thumbnail, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                video_id,
                meta["title"] or "Unknown Title",
                meta["channel"] or "Unknown Channel",
                meta["published"], meta["duration"],
                transcript_obj.language_code,
                text, summary,
                json.dumps(meta["chapters"]),
                embedding, json.dumps(raw_data),
                meta["thumbnail"], datetime.now(),
            ))

        return {
            "video_id": video_id,
            "title": meta["title"] or "Unknown Title",
            "channel": meta["channel"] or "Unknown Channel",
            "language": transcript_obj.language_code,
            "transcript": text, "summary": summary,
            "chapters": meta["chapters"],
            "fetched_at": str(datetime.now()),
            "thumbnail": meta["thumbnail"],
            "cached": False,
        }

    except (TranscriptsDisabled, NoTranscriptFound):
        raise HTTPException(404, "No transcripts available for this video")
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")


@app.get("/search")
def search_library(q: str = Query(..., min_length=2), limit: int = 20):
    with _db_lock:
        rows = con.execute("""
            SELECT video_id, title, channel, transcript
            FROM transcripts
            WHERE LOWER(transcript) LIKE LOWER('%' || ? || '%')
            LIMIT ?
        """, (q, limit)).fetchall()

    results = []
    for vid_id, title, channel, transcript in rows:
        idx = transcript.lower().find(q.lower())
        snippet = transcript[max(0, idx - 80): idx + 120] if idx >= 0 else transcript[:200]
        results.append({"video_id": vid_id, "title": title, "channel": channel, "snippet": snippet.strip() + "…"})

    return {"query": q, "total": len(results), "results": results}


@app.get("/semantic_search")
def semantic_search(q: str = Query(..., min_length=2), limit: int = 10):
    query_emb = embedder.encode(q).tolist()
    with _db_lock:
        rows = con.execute("""
            SELECT video_id, title, channel, summary,
                   array_cosine_similarity(embedding, ?::FLOAT[384]) AS similarity
            FROM transcripts
            ORDER BY similarity DESC
            LIMIT ?
        """, (query_emb, limit)).fetchall()

    return {"query": q, "results": [
        {"video_id": r[0], "title": r[1], "channel": r[2], "summary": r[3], "similarity": round(float(r[4]), 4)}
        for r in rows
    ]}


@app.get("/hybrid_search")
def hybrid_search(q: str = Query(..., min_length=2), limit: int = 10):
    # Keyword-ranked IDs (substring match on transcript).
    with _db_lock:
        keyword_rows = con.execute("""
            SELECT video_id FROM transcripts
            WHERE LOWER(transcript) LIKE LOWER('%' || ? || '%')
            LIMIT 50
        """, (q,)).fetchall()
    keyword_ids = [r[0] for r in keyword_rows]

    # Semantic-ranked IDs (cosine similarity on summary embedding).
    query_emb = embedder.encode(q).tolist()
    with _db_lock:
        semantic_rows = con.execute("""
            SELECT video_id,
                   array_cosine_similarity(embedding, ?::FLOAT[384]) AS similarity
            FROM transcripts ORDER BY similarity DESC LIMIT 50
        """, (query_emb,)).fetchall()
    semantic_ids = [r[0] for r in semantic_rows]

    fused = rrf_fuse(keyword_ids, semantic_ids)[:limit]
    if not fused:
        return {"query": q, "results": []}

    placeholders = ", ".join("?" for _ in fused)
    with _db_lock:
        detail = con.execute(f"""
            SELECT video_id, title, channel, summary
            FROM transcripts WHERE video_id IN ({placeholders})
        """, fused).fetchall()
    by_id = {r[0]: r for r in detail}
    results = [
        {"video_id": vid, "title": by_id[vid][1],
         "channel": by_id[vid][2], "summary": by_id[vid][3]}
        for vid in fused if vid in by_id
    ]
    return {"query": q, "results": results}


@app.get("/library")
def list_library(category: Optional[str] = Query(None)):
    if category:
        with _db_lock:
            rows = con.execute("""
                SELECT video_id, title, channel, language, duration, fetched_at, category
                FROM transcripts WHERE category = ? ORDER BY fetched_at DESC
            """, (category,)).fetchall()
    else:
        with _db_lock:
            rows = con.execute("""
                SELECT video_id, title, channel, language, duration, fetched_at, category
                FROM transcripts ORDER BY fetched_at DESC
            """).fetchall()
    return {"total": len(rows), "library": [
        {"video_id": r[0], "title": r[1], "channel": r[2], "language": r[3],
         "duration_sec": r[4], "fetched_at": str(r[5]), "category": r[6]}
        for r in rows
    ]}


@app.delete("/library/{video_id}")
def delete_transcript(video_id: str):
    with _db_lock:
        exists = con.execute("SELECT video_id FROM transcripts WHERE video_id = ?", (video_id,)).fetchone()
    if not exists:
        raise HTTPException(404, f"Video '{video_id}' not in library")
    with _db_lock:
        con.execute("DELETE FROM transcripts WHERE video_id = ?", (video_id,))
    return {"status": "deleted", "video_id": video_id}


@app.patch("/library/{video_id}")
def update_category(video_id: str, req: CategoryUpdate):
    with _db_lock:
        exists = con.execute("SELECT video_id FROM transcripts WHERE video_id = ?", (video_id,)).fetchone()
    if not exists:
        raise HTTPException(404, f"Video '{video_id}' not in library")
    category = req.category.strip() or "Uncategorized"
    with _db_lock:
        con.execute("UPDATE transcripts SET category = ? WHERE video_id = ?", (category, video_id))
    return {"status": "updated", "video_id": video_id, "category": category}


@app.get("/export/{video_id}")
def export_markdown(video_id: str):
    with _db_lock:
        row = con.execute("""
            SELECT title, channel, published, duration, language,
                   transcript, summary, chapters, thumbnail
            FROM transcripts WHERE video_id = ?
        """, (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Video not in library")

    title, channel, published, duration, language, transcript, summary, chapters_json, thumbnail = row
    chapters = json.loads(chapters_json) if chapters_json else []
    dur_str = f"{duration // 60}m {duration % 60}s" if duration else "unknown"
    pub_str = (
        f"{published[:4]}-{published[4:6]}-{published[6:]}"
        if published and len(published) == 8 else (published or "unknown")
    )

    md = f"# {title}\n\n"
    md += f"**Channel:** {channel}  \n**Published:** {pub_str}  \n**Duration:** {dur_str}  \n**Language:** {language}\n\n"
    if thumbnail:
        md += f"![thumbnail]({thumbnail})\n\n"
    md += f"## Summary\n\n{summary}\n\n"
    if chapters:
        md += "## Chapters\n\n"
        for ch in chapters:
            t = int(ch.get("start_time", 0))
            md += f"- **{ch.get('title')}** ({t // 60}:{t % 60:02d})\n"
        md += "\n"
    md += f"## Full Transcript\n\n{transcript}\n"

    safe_title = re.sub(r"[^\w\s-]", "", title or video_id)[:50].strip().replace(" ", "_")
    return {"markdown": md, "filename": f"{video_id}_{safe_title}.md"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
