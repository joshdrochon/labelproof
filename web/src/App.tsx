/**
 * The frame: masthead, the two modes, and the screen itself.
 *
 * The masthead names the agency and the tool, in that order, because the first question
 * a cold user asks is "what am I looking at". There is no login, no settings, no
 * onboarding, and nothing to configure before the first useful answer (UX-7).
 *
 * Batch check is a second screen owned elsewhere in this build. Its tab is present and
 * plainly marked as not available here, rather than silently missing — a tab that leads
 * nowhere is worse than a tab that says so.
 */

import VerifyNow from './routes/VerifyNow';

export default function App() {
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
        </div>
      </header>

      <nav className="tabs" aria-label="Modes">
        <ul className="tabs__list">
          <li>
            <span className="tab" aria-current="page">
              Verify now
            </span>
          </li>
          <li>
            <button type="button" className="tab tab--unavailable" disabled>
              Batch check
              <span className="visually-hidden">
                {' '}
                — not available on this screen
              </span>
            </button>
          </li>
        </ul>
      </nav>

      <main className="main" id="main">
        <VerifyNow />
      </main>

      <footer className="site-footer">
        <p>
          A prototype. It compares a label against an application and hands you a
          checklist — it does not approve, reject, or file anything.
        </p>
      </footer>
    </div>
  );
}
