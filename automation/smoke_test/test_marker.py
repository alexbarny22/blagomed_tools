from pathlib import Path


def test_ai_orchestrator_smoke_marker():
    readme = Path(__file__).with_name('README.md').read_text(encoding='utf-8')
    assert 'AI_ORCHESTRATOR_SMOKE_TEST_OK' in readme
