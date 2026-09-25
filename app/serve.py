"""Container entrypoint: start warming the models in a background thread, then run the Streamlit server
in the same process so the warmed singleton (movie_agent.runtime) is what every session uses."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from movie_agent.runtime import warm_in_background  # noqa: E402

if __name__ == "__main__":
    warm_in_background()
    from streamlit.web import cli as stcli
    sys.argv = ["streamlit", "run", str(ROOT / "app" / "streamlit_app.py"), "--server.address=0.0.0.0",
                "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false", *sys.argv[1:]]
    sys.exit(stcli.main())
