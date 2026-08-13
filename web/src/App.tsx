/**
 * The frame: masthead, the two modes, and the screen itself.
 *
 * The masthead names the agency and the tool, in that order, because the first question
 * a cold user asks is "what am I looking at". There is no login, no settings, no
 * onboarding, and nothing to configure before the first useful answer (UX-7).
 *
 * Batch check is a real tab (LP-167). It was a disabled stub while the screen did not
 * exist, which was the honest thing to show at the time — Janet has been asking for batch
 * for years, and burying it behind a menu or leaving it dead would have been the wrong
 * kind of quiet.
 *
 * Mode lives in the URL path rather than in component state so a batch screen survives a
 * refresh and can be linked to. The server falls back to the app shell on unknown paths,
 * so a client-side route loads directly.
 *
 * The path is `/batch-check`, NOT `/batch`. `/batch` is the API endpoint that queues a
 * job: it accepts POST and answers a browser's GET with 405, so using it here would have
 * produced a screen that worked until someone pressed reload or shared the link — the two
 * things a URL is for. A UI route and an API route must not share a path.
 * `tests/contract/test_http_ui_contract.py` pins both halves.
 */

import { useEffect, useState } from 'react';

import VerifyNow from './routes/VerifyNow';
import BatchCheck from './routes/BatchCheck';

type Mode = 'verify' | 'batch';

const PATHS: Record<Mode, string> = { verify: '/', batch: '/batch-check' };

function modeFromPath(path: string): Mode {
  return path.startsWith('/batch-check') ? 'batch' : 'verify';
}

export default function App() {
  const [mode, setMode] = useState<Mode>(() =>
    modeFromPath(typeof window === 'undefined' ? '/' : window.location.pathname),
  );

  // Back and forward have to work. A tool that traps you on one screen teaches people to
  // reload, and reloading a running batch is how an agent loses their place.
  useEffect(() => {
    const onPop = () => setMode(modeFromPath(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const go = (next: Mode) => {
    if (next === mode) return;
    window.history.pushState(null, '', PATHS[next]);
    setMode(next);
  };

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to the checklist
      </a>

      <header className="masthead">
        <div className="masthead__inner">
          <div className="masthead__seal" aria-hidden="true">
            TTB
          </div>
          <div className="masthead__titles">
            <p className="masthead__product">Label Verification</p>
            <p className="masthead__agency">
              Alcohol and Tobacco Tax and Trade Bureau
            </p>
          </div>
          {/* OUT of the title stack and over to the right. This page carries a real
              federal agency's name on a public URL, so saying plainly that it is neither
              theirs nor endorsed by them is the minimum — and it belongs in the masthead
              rather than the footer, because a disclaimer nobody scrolls to is a
              disclaimer for the author's benefit. But stacked as a third line it made the
              masthead read as three competing titles. On the right it is unmistakably an
              aside, and the masthead is two lines again. */}
          <p className="masthead__disclaimer">
            Prototype — not affiliated with or endorsed by TTB
          </p>
        </div>
      </header>

      <nav className="tabs" aria-label="Modes">
        <ul className="tabs__list">
          <li>
            <button
              type="button"
              className="tab"
              aria-current={mode === 'verify' ? 'page' : undefined}
              onClick={() => go('verify')}
            >
              Verify now
            </button>
          </li>
          <li>
            <button
              type="button"
              className="tab"
              aria-current={mode === 'batch' ? 'page' : undefined}
              onClick={() => go('batch')}
            >
              Batch check
            </button>
          </li>
        </ul>
      </nav>

      <main className="main" id="main">
        {mode === 'batch' ? <BatchCheck /> : <VerifyNow />}
      </main>

      <footer className="site-footer">
        <p>
          A prototype. It compares a label against an application and hands you a
          checklist — it does not approve, reject, or file anything.
        </p>
        {/* SEC-1 and SCOPE-4, on screen rather than only in the README. Someone
            evaluating this needs to know what they are looking at without reading a
            document, and "no real applicant data" is a claim worth making where the
            data would be. */}
        <p className="site-footer__notice">
          Every label here is synthetic or a photograph of a retail bottle. No applicant
          data, no personal data, and nothing you upload is kept — images and results are
          deleted within 24 hours.
        </p>
      </footer>
    </div>
  );
}
