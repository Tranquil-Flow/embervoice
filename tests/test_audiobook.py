from __future__ import annotations

import math
import json
import subprocess
import wave
from pathlib import Path

import pytest
from ebooklib import epub

from audiobook import convert, extract_book, split_passages


def sample_epub(path: Path, title: str = "A Free World") -> Path:
    book = epub.EpubBook()
    book.set_identifier("sample-1")
    book.set_title(title)
    book.set_language("en")
    book.add_author("A. Reader")
    intro = epub.EpubHtml(title="Introduction", file_name="intro.xhtml", lang="en")
    intro.content = '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Introduction</h1><p>Hello, reader. This is the opening.</p><p class="pagenum">12</p><p>We begin here.</p></body></html>'
    first = epub.EpubHtml(title="Chapter One", file_name="chapter1.xhtml", lang="en")
    first.content = '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter One</h1><p>Freedom begins with care. We build it together.</p><script>Do not read this</script></body></html>'
    book.add_item(intro)
    book.add_item(first)
    book.toc = (epub.Link("intro.xhtml", "Introduction", "intro"), epub.Link("chapter1.xhtml", "Chapter One", "one"))
    book.spine = ["nav", intro, first]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)
    return path


def tone(wav_path: Path, text: str, style: str, seed: int) -> None:
    assert style == "Warm, measured narrator"
    assert text and seed >= 0
    import array

    samples = array.array("h", (round(2000 * math.sin(i * 440 * math.tau / 24000)) for i in range(4800)))
    with wave.open(str(wav_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(samples.tobytes())


def test_extract_spine_chapters_and_filter_markup(tmp_path):
    book = extract_book(sample_epub(tmp_path / "test.epub"))
    assert book.title == "A Free World"
    assert book.author == "A. Reader"
    assert [c.title for c in book.chapters] == ["Introduction", "Chapter One"]
    assert "Hello, reader" in book.chapters[0].text
    assert "pagenum" not in book.chapters[0].text
    assert "12" not in book.chapters[0].text
    assert "Do not read this" not in book.chapters[1].text


def test_preview_selected_later_passage_only_and_resume(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("selected-preview")
    book.set_title("Long Chapter")
    book.set_language("en")
    chapter = epub.EpubHtml(title="Chapter One", file_name="one.xhtml", lang="en")
    chapter.content = (
        "<html><body><h1>Chapter One</h1><p>" + ("Freedom and care grow together. " * 55) + "</p></body></html>"
    )
    book.add_item(chapter)
    book.toc = (epub.Link("one.xhtml", "Chapter One", "one"),)
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    source = tmp_path / "long.epub"
    epub.write_epub(str(source), book)
    out = tmp_path / "audio"
    assert len(split_passages(extract_book(source).chapters[0].text)) >= 3
    result = convert(
        source,
        out,
        "Warm, measured narrator",
        synth=tone,
        max_chapters=1,
        max_passages=1,
        start_chapter=1,
        preview_passage=3,
    )
    assert result["passages_generated"] == 1
    assert sorted(p.name for p in (out / "passages").glob("*.wav")) == ["001-0003.wav"]
    assert not list((out / "chapters").glob("*.m4a")) and not list(out.glob("*.m4b"))
    again = convert(
        source,
        out,
        "Warm, measured narrator",
        synth=lambda *a: (_ for _ in ()).throw(AssertionError("cached")),
        max_chapters=1,
        max_passages=1,
        start_chapter=1,
        preview_passage=3,
    )
    assert again["passages_generated"] == 0
    with pytest.raises(ValueError, match="passage"):
        convert(
            source,
            out,
            "Warm, measured narrator",
            synth=tone,
            max_chapters=1,
            max_passages=1,
            start_chapter=1,
            preview_passage=999,
        )


def test_corrected_passage_is_new_version_and_reuses_other_audio(tmp_path):
    source = sample_epub(tmp_path / "book.epub")
    original = tmp_path / "original"
    convert(source, original, "Warm, measured narrator", synth=tone)
    old_wav = (original / "passages" / "002-0001.wav").read_bytes()
    generated = []

    def revised_synth(path, text, style, seed):
        generated.append((text, seed))
        tone(path, text, style, seed)

    new = tmp_path / "corrected"
    text = "Freedom begins with mutual care. We build it together."
    result = convert(
        source,
        new,
        "Warm, measured narrator",
        synth=revised_synth,
        base_output=original,
        retry_chapter=2,
        retry_passage=1,
        retry_text=text,
        retry_attempt=0,
    )
    assert result["complete"] and result["passages_generated"] == 1
    assert generated[0][0] == text
    assert (new / "passages" / "001-0001.wav").read_bytes() == (original / "passages" / "001-0001.wav").read_bytes()
    assert (original / "passages" / "002-0001.wav").read_bytes() == old_wav
    assert json.loads((new / "manifest.json").read_text())["corrections"] == {"002-0001": text}
    assert (new / "chapters" / "001-Introduction.m4a").read_bytes() == (
        original / "chapters" / "001-Introduction.m4a"
    ).read_bytes()
    assert len(list(new.glob("*.m4b"))) == 1
    assert (
        convert(
            source,
            new,
            "Warm, measured narrator",
            synth=revised_synth,
            base_output=original,
            retry_chapter=2,
            retry_passage=1,
            retry_text=text,
            retry_attempt=0,
        )["passages_generated"]
        == 0
    )
    next_version = tmp_path / "another-try"
    convert(
        source,
        next_version,
        "Warm, measured narrator",
        synth=revised_synth,
        base_output=original,
        retry_chapter=2,
        retry_passage=1,
        retry_text=text,
        retry_attempt=1,
    )
    assert len(generated) == 2 and generated[0][1] != generated[1][1]


def test_split_preserves_all_words_without_truncation():
    text = "First sentence. Second sentence.\n\nThird sentence has a few more words."
    parts = split_passages(text, max_chars=32)
    assert all(len(p) <= 32 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())
    with pytest.raises(ValueError, match="too long"):
        split_passages("supercalifragilisticexpialidocious", max_chars=10)


def test_end_to_end_chapters_book_and_resume(tmp_path):
    source = sample_epub(tmp_path / "test.epub")
    out = tmp_path / "audio"
    stats = convert(source, out, "Warm, measured narrator", synth=tone, max_chars=90)
    assert stats["chapters"] == 2
    assert stats["passages_generated"] >= 2
    assert (out / "A Free World.m4b").is_file()
    assert len(list((out / "chapters").glob("*.m4a"))) == 2
    assert len(list((out / "passages").glob("*.wav"))) >= 2
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "chapter_tags=title",
            "-of",
            "default=noprint_wrappers=1",
            str(out / "A Free World.m4b"),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "Introduction" in probe.stdout and "Chapter One" in probe.stdout
    second = convert(
        source,
        out,
        "Warm, measured narrator",
        synth=lambda *args: (_ for _ in ()).throw(AssertionError("should resume")),
        max_chars=90,
    )
    assert second["passages_generated"] == 0
    with pytest.raises(ValueError, match="different settings"):
        convert(source, out, "A new narrator", synth=tone, max_chars=90)


def test_failure_does_not_mark_incomplete_audio_complete(tmp_path):
    source = sample_epub(tmp_path / "test.epub")
    out = tmp_path / "audio"

    def broken(path, text, style, seed):
        path.write_bytes(b"broken")
        raise RuntimeError("synthesis failed")

    with pytest.raises(RuntimeError, match="synthesis failed"):
        convert(source, out, "Warm, measured narrator", synth=broken)
    assert not list((out / "passages").glob("*.wav"))
    assert not list(out.glob("*.m4b"))
    assert not list((out / "chapters").glob("*.m4a"))


def test_toc_anchors_split_a_single_spine_document(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("same-file")
    book.set_title("One file")
    book.set_language("en")
    body = epub.EpubHtml(title="All", file_name="all.xhtml", lang="en")
    body.content = '<html><body><h1 id="start">First</h1><p>First body.</p><h2 id="next">Second</h2><p>Second body.</p></body></html>'
    book.add_item(body)
    book.toc = (epub.Link("all.xhtml#start", "First", "start"), epub.Link("all.xhtml#next", "Second", "next"))
    book.spine = ["nav", body]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    source = tmp_path / "anchors.epub"
    epub.write_epub(str(source), book)
    chapters = extract_book(source).chapters
    assert [c.title for c in chapters] == ["First", "Second"]
    assert "Second body" not in chapters[0].text
    assert "First body" not in chapters[1].text


def test_partial_run_resumes_without_regenerating_first_passage(tmp_path):
    source = sample_epub(tmp_path / "test.epub")
    out = tmp_path / "audio"
    part = convert(source, out, "Warm, measured narrator", synth=tone, max_passages=1)
    assert not part["complete"] and part["passages_generated"] == 1
    first = out / "passages" / "001-0001.wav"
    before = first.stat().st_mtime_ns
    repeated = convert(
        source,
        out,
        "Warm, measured narrator",
        synth=lambda *args: (_ for _ in ()).throw(AssertionError("sample limit moved")),
        max_passages=1,
    )
    assert repeated["passages_generated"] == 0
    assert len(list((out / "passages").glob("*.wav"))) == 1
    final = convert(source, out, "Warm, measured narrator", synth=tone)
    assert final["complete"] and final["passages_generated"] >= 1
    assert first.stat().st_mtime_ns == before


def test_inspect_cli_does_not_require_a_style_or_load_the_model(tmp_path):
    source = sample_epub(tmp_path / "test.epub")
    import sys

    run = subprocess.run(
        [sys.executable, "-m", "audiobook", str(source), "--inspect"], check=True, capture_output=True, text=True
    )
    assert '"chapters"' in run.stdout and "Chapter One" in run.stdout
    assert not (tmp_path / "audio").exists()


def test_multi_passage_join_and_metadata_escaping(tmp_path):
    source = sample_epub(tmp_path / "test.epub", title="A; #= World")
    out = tmp_path / "audio"
    stats = convert(source, out, "Warm, measured narrator", synth=tone, max_chars=25)
    assert stats["passages_generated"] > 2
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags=title:chapter_tags=title",
            "-of",
            "json",
            str(out / "A; #= World.m4b"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    metadata = json.loads(result.stdout)
    assert metadata["format"]["tags"]["title"] == "A; #= World"
    assert len(metadata["chapters"]) == 2
    chapter = out / "chapters" / "001-Introduction.m4a"
    duration = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(chapter),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(duration.stdout) > 0.4  # two 0.2s tones separated by a pause


def test_cli_refuses_another_studio_gpu_job_before_writing_output(tmp_path, monkeypatch):
    import sys
    from audiobook import gpu_slot

    monkeypatch.setenv("HOME", str(tmp_path))
    source = sample_epub(tmp_path / "book.epub")
    output = tmp_path / "must-not-exist"
    with gpu_slot():
        run = subprocess.run(
            [sys.executable, "-m", "audiobook", str(source), "--style", "A calm narrator", "--output", str(output)],
            capture_output=True,
            text=True,
        )
    assert run.returncode != 0 and "holds the GPU lane" in run.stderr
    assert not output.exists()


def test_completed_book_integrity_auditor(tmp_path):
    from verify_book import verify

    source = sample_epub(tmp_path / "book.epub")
    output = tmp_path / "audio"
    convert(source, output, "Warm, measured narrator", synth=tone)
    receipt = verify(source, output)
    assert receipt["chapters"] == 2
    assert receipt["passages"] >= 2
    assert receipt["chapter_titles"] == ["Introduction", "Chapter One"]
    assert receipt["decoded_peak"] > 100


def test_title_page_marker_is_distinct_from_essay_without_changing_text(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("title-page")
    book.set_title("On Vegetarianism")
    book.set_language("en")
    front = epub.EpubHtml(title="On Vegetarianism", file_name="titlepage.xhtml", lang="en")
    front.content = "<html><body><h1>On Vegetarianism</h1><p>1901</p></body></html>"
    essay = epub.EpubHtml(title="On Vegetarianism", file_name="piece.xhtml", lang="en")
    essay.content = "<html><body><h1>On Vegetarianism</h1><p>Food and freedom.</p></body></html>"
    book.add_item(front)
    book.add_item(essay)
    book.toc = (
        epub.Link("titlepage.xhtml", "On Vegetarianism", "front"),
        epub.Link("piece.xhtml", "On Vegetarianism", "essay"),
    )
    book.spine = ["nav", front, essay]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    source = tmp_path / "titles.epub"
    epub.write_epub(str(source), book)
    out = tmp_path / "audio"
    convert(source, out, "Warm, measured narrator", synth=tone)
    from verify_book import verify

    receipt = verify(source, out)
    assert receipt["chapter_titles"] == ["Title page", "On Vegetarianism"]


def test_progress_stages_precede_slow_model_and_passage_then_encode(tmp_path, monkeypatch, capsys):
    import json
    import audiobook

    source = sample_epub(tmp_path / "book.epub")
    model = tmp_path / "local-model"
    model.mkdir()
    calls = []
    emitted = []

    def fake_model(*_args):
        calls.append("load")
        # Event must have been flushed before expensive model loading.
        emitted.append(capsys.readouterr().out)
        assert '"stage": "loading"' in emitted[-1]

        def speak(*args):
            calls.append("synthesize")
            emitted.append(capsys.readouterr().out)
            assert '"stage": "synthesizing"' in emitted[-1]
            tone(*args)

        return speak

    monkeypatch.setattr(audiobook, "mlx_synth", fake_model)
    result = convert(source, tmp_path / "out", "Warm, measured narrator", model_path=model, max_passages=1)
    emitted.append(capsys.readouterr().out)
    events = [
        json.loads(line.removeprefix("STUDIO_EVENT "))
        for line in "".join(emitted).splitlines()
        if line.startswith("STUDIO_EVENT ")
    ]
    assert calls == ["load", "synthesize"]
    assert [event["stage"] for event in events] == ["planning", "loading", "synthesizing", "saved", "encoding"]
    assert events[2]["chapter"] == 1 and events[2]["passage"] == 1
    assert events[0]["total_passages"] == 2
    assert events[0]["total_characters"] == sum(
        len(part) for chapter in extract_book(source).chapters for part in split_passages(chapter.text)
    )
    assert events[2]["characters"] == events[3]["characters"] > 0
    assert not result["complete"]
    assert not any(event["stage"] == "assembling" for event in events)


def test_full_run_reports_encoding_and_assembly_without_reloading(tmp_path, capsys):
    source = sample_epub(tmp_path / "book.epub")
    result = convert(source, tmp_path / "out", "Warm, measured narrator", synth=tone)
    stages = [
        __import__("json").loads(line.removeprefix("STUDIO_EVENT "))["stage"]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("STUDIO_EVENT ")
    ]
    assert result["complete"]
    assert stages.count("encoding") == 2
    assert stages[-2:] == ["assembling", "ready"]
    assert "loading" not in stages


def test_studio_quiet_summary_keeps_progress_but_not_verbose_result(tmp_path):
    import os
    import sys

    source = sample_epub(tmp_path / "book.epub")
    output = tmp_path / "audio"
    convert(source, output, "Warm, measured narrator", synth=tone)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "audiobook",
            str(source),
            "--output",
            str(output),
            "--style",
            "Warm, measured narrator",
            "--quiet-summary",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert run.returncode == 0, run.stderr
    assert "STUDIO_EVENT " in run.stdout
    assert '"chapters_complete"' not in run.stdout


def test_resumed_passage_reports_its_work_without_remeasuring(tmp_path, capsys):
    import json

    source = sample_epub(tmp_path / "book.epub")
    output = tmp_path / "audio"
    convert(source, output, "Warm, measured narrator", synth=tone, max_passages=1)
    capsys.readouterr()
    convert(source, output, "Warm, measured narrator", synth=tone, max_passages=1)
    events = [
        json.loads(line.removeprefix("STUDIO_EVENT "))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("STUDIO_EVENT ")
    ]
    reused = [event for event in events if event["stage"] == "reused"]
    assert len(reused) == 1 and reused[0]["characters"] > 0
    assert not any(event["stage"] == "saved" for event in events)


def test_memory_tiers_refuse_hard_limits_and_require_opt_in_below_recommended():
    from audiobook import check_memory

    roomy = {"available_gib": 20, "total_gib": 48, "swap_fraction": 0.1}
    check_memory(roomy)
    check_memory(None)  # no readings on this platform: nothing to judge
    low = {**roomy, "available_gib": 9.0}
    with pytest.raises(RuntimeError, match=r"Low memory: 9.0 GiB free of 12.5 GiB recommended"):
        check_memory(low)
    check_memory(low, allow_low_memory=True)
    for snapshot, reason in [
        ({**roomy, "total_gib": 8}, "at least 12 GiB"),
        ({**roomy, "available_gib": 3.2}, "at least 4 GiB"),
        ({**roomy, "swap_fraction": 0.95}, "Swap pressure"),
    ]:
        with pytest.raises(RuntimeError, match=reason):
            check_memory(snapshot, allow_low_memory=True)


def test_critical_memory_pressure_stops_before_the_next_passage_and_keeps_saved_audio(tmp_path, monkeypatch):
    import audiobook

    source = sample_epub(tmp_path / "book.epub")
    readings = iter([False, True])
    monkeypatch.setattr(audiobook, "memory_pressure_critical", lambda: next(readings))
    with pytest.raises(RuntimeError, match="Memory pressure critical"):
        convert(source, tmp_path / "out", "Warm, measured narrator", synth=tone)
    assert [p.name for p in (tmp_path / "out" / "passages").glob("*.wav")] == ["001-0001.wav"]


def test_low_memory_opt_in_reaches_the_model_loader(tmp_path, monkeypatch):
    import audiobook

    source = sample_epub(tmp_path / "book.epub")
    model = tmp_path / "model"
    model.mkdir()
    seen = []

    def fake_model(*_args, allow_low_memory=False):
        seen.append(allow_low_memory)
        return tone

    monkeypatch.setattr(audiobook, "mlx_synth", fake_model)
    convert(
        source, tmp_path / "out", "Warm, measured narrator", model_path=model, max_passages=1, allow_low_memory=True
    )
    assert seen == [True]


def test_voice_clip_softens_the_written_direction_and_marks_only_clip_versions(tmp_path, monkeypatch):
    import sys, types, numpy as np, audiobook

    seen = []

    class Model:
        def generate(self, **kw):
            seen.append((kw["cfg_scale"], kw["ref_audio"]))
            yield types.SimpleNamespace(
                audio=np.full(2400, 0.1, np.float32), sample_rate=audiobook.SAMPLE_RATE, token_count=10
            )

    monkeypatch.setattr(audiobook, "check_memory", lambda *a: None)
    monkeypatch.setattr(audiobook, "memory_snapshot", lambda: None)
    from setup_fakes import ReadySetup

    monkeypatch.setattr(audiobook, "ModelSetup", lambda *a: ReadySetup())
    monkeypatch.setitem(sys.modules, "mlx_audio.tts", types.SimpleNamespace(load=lambda path: Model()))
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"x")
    audiobook.mlx_synth(tmp_path, clip, "Exact words")(tmp_path / "a.wav", "Hello.", "Warm", 1)
    audiobook.mlx_synth(tmp_path)(tmp_path / "b.wav", "Hello.", "Warm", 1)
    assert seen == [(audiobook.CLIP_CFG, str(clip)), (audiobook.STYLE_CFG, None)]
    assert audiobook.CLIP_CFG < audiobook.STYLE_CFG

    source = sample_epub(tmp_path / "book.epub")
    monkeypatch.setattr(audiobook, "mlx_synth", lambda *a, **kw: tone)
    convert(source, tmp_path / "plain", "Warm, measured narrator", model_path=tmp_path, max_passages=1)
    convert(
        source,
        tmp_path / "clip",
        "Warm, measured narrator",
        model_path=tmp_path,
        max_passages=1,
        ref_audio=clip,
        ref_text="Exact words",
    )
    assert "clip_cfg" not in json.loads((tmp_path / "plain/manifest.json").read_text())
    assert json.loads((tmp_path / "clip/manifest.json").read_text())["clip_cfg"] == audiobook.CLIP_CFG
