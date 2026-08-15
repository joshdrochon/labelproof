/**
 * The checked screen — the product's primary path, and until now the only screen with no
 * test at all.
 *
 * The hole was total and worth stating plainly: there was no `VerifyNow.test.tsx`;
 * `a11y.test.tsx` rendered `<App />` and left it sitting at the setup phase; `e2e/a11y.spec.ts`
 * audits `/` and `/batch-check` only; and the one place `e2e/errors.spec.ts` mentions the
 * checklist it asserts a count of ZERO, because it is checking that a refused upload
 * produces no verdicts. Nothing anywhere rendered a verdict successfully. Three real bugs
 * shipped through it — a skipped heading level, a picture that moved on hover, and an
 * unclamped image index that could take the label off the screen for good — and every one
 * of them was invisible to CI. Someone could have deleted the recommendation banner and
 * the suite would have stayed green.
 *
 * These drive the REAL component to `phase === 'checked'` over a stubbed `fetch`, using
 * the sample path — `loadSample` renders a payload that already carries `aggregate` and
 * `fields` without a `/verify` round trip. Nothing here asserts against a stub of the
 * screen: the thing under test is the screen an agent reads.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';

import VerifyNow from './VerifyNow';
import { fieldResult, samplePayload, sevenFields } from '../testing';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Stub `/sample` and drive the screen to a verdict the way a reviewer would. */
async function reachChecked(payload: Record<string, unknown> = samplePayload()) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    ),
  );

  const user = userEvent.setup();
  const view = render(<VerifyNow />);

  const sample = await screen.findByRole('button', { name: /old tom/i });
  await user.click(sample);
  // The banner is the marker for "this screen is now showing a verdict".
  await screen.findByRole('region', { name: /recommendation/i });
  return { user, view };
}

describe('the verdict itself', () => {
  it('names the recommendation in words, with its rationale', async () => {
    await reachChecked();

    const banner = within(screen.getByRole('region', { name: /recommendation/i }));
    expect(banner.getByText('Return for correction')).toBeInTheDocument();
    expect(banner.getByText(/government warning does not match/i)).toBeInTheDocument();
    // "Recommendation:" is fixed and never dropped — the app advises, the agent decides.
    expect(banner.getByText(/recommendation/i)).toBeInTheDocument();
  });

  it('renders one checklist row per field, and loses none of them', async () => {
    await reachChecked();

    // Seven fields in, seven rows out. A row silently missing from a compliance checklist
    // is the worst failure this screen has.
    const rows = screen.getAllByTestId(/^row-/);
    expect(rows).toHaveLength(sevenFields().length);
    // Scoped to the checklist: the banner names attention rows too, and the picture's
    // legend names every outlined one, so an unscoped lookup would pass on those alone.
    const checklist = within(screen.getByRole('table'));
    for (const label of [
      'Brand name',
      'Class / type',
      'Alcohol content',
      'Net contents',
      'Producer name and address',
      'Country of origin',
      'Government warning',
    ]) {
      expect(checklist.getByText(label)).toBeInTheDocument();
    }
  });

  it('shows the elapsed time, because trust in a fast answer is partly trust it ran', async () => {
    await reachChecked();
    expect(screen.getByTestId('elapsed')).toHaveTextContent(/2\.8s/);
  });
});

describe('the evidence panel', () => {
  it('draws the picture and numbers the row that needs eyes', async () => {
    await reachChecked();

    expect(screen.getByRole('img', { name: /front/i })).toHaveAttribute(
      'src',
      '/sample/images/front.png',
    );
    // The one attention row is outlined; the six that agree are not drawn over.
    expect(screen.getByTestId('region-government_warning')).toBeInTheDocument();
    expect(screen.queryByTestId('region-brand_name')).not.toBeInTheDocument();
  });

  it('gives the checklist and the picture the SAME number for a row', async () => {
    // The two numbers come from different call sites and drifted apart in the batch
    // drill-in for exactly this reason. If they disagree, "outlined as 2" sends an agent
    // to the wrong words on the label — worse than no outline, because it is trusted.
    await reachChecked();

    // The MARKER on the photo, not the legend entry: the marker is the thing an agent's
    // eye lands on, and it is what "outlined as N" is a claim about. It is the control
    // whose whole visible content is the number.
    const marker = within(screen.getByRole('figure'))
      .getAllByRole('button', { name: /government warning/i })
      .find((button) => /^\d+$/.test(button.textContent?.trim() ?? ''));
    const onPicture = marker?.textContent?.trim();
    const inChecklist = within(screen.getByTestId('row-government_warning'))
      .getByText(/outlined as \d+ on the picture/i)
      .textContent?.match(/\d+/)?.[0];

    expect(onPicture).toBeTruthy();
    expect(inChecklist).toBe(onPicture);
  });

  it('numbers from the regions, so a row with no outline does not shift the rest', async () => {
    // A `missing` government warning carries no bbox and sorts FIRST. Numbering by
    // position in the attention list would hand the row below it the number 2 while the
    // only outline on the picture is numbered 1.
    await reachChecked(
      samplePayload({
        fields: [
          fieldResult('government_warning', 'missing', { evidence: null }),
          fieldResult('brand_name', 'mismatch'),
        ],
      }),
    );

    expect(
      within(screen.getByTestId('row-brand_name')).getByText(/outlined as 1 on the picture/i),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId('row-government_warning')).queryByText(/outlined as/i),
    ).not.toBeInTheDocument();
  });
});

describe('a two-sided label', () => {
  const twoPictures = () =>
    samplePayload({
      pictures: [
        { role: 'front', url: '/sample/images/front.png' },
        { role: 'back', url: '/sample/images/back.png' },
      ],
      fields: [
        fieldResult('brand_name', 'mismatch'),
        fieldResult('government_warning', 'mismatch', {
          evidence: { image_index: 1, bbox: { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 } },
        }),
      ],
    });

  it('switches pictures when the agent asks', async () => {
    const { user } = await reachChecked(twoPictures());

    const group = within(screen.getByRole('group', { name: /which picture/i }));
    await user.click(group.getByRole('button', { name: 'back' }));

    expect(screen.getByRole('img', { name: /back/i })).toHaveAttribute(
      'src',
      '/sample/images/back.png',
    );
  });

  it('HOLDS the agent\'s choice while they read the checklist', async () => {
    // The regression that prompted this file. Hover fires `FieldRow.onActivate`, and
    // wiring the picture switch to it meant a mouse crossing any front row snapped the
    // back picture away — so a two-sided label could not be held still long enough to
    // read the row you turned to it for.
    const { user } = await reachChecked(twoPictures());

    // Turn to the back deliberately, then read a row whose evidence is on the FRONT.
    // Hovering a row that happens to point at the picture already showing would prove
    // nothing — the sequence has to end on the row that would pull it away.
    await user.click(
      within(screen.getByRole('group', { name: /which picture/i })).getByRole('button', {
        name: 'back',
      }),
    );

    await user.hover(screen.getByTestId('row-brand_name'));
    expect(screen.getByRole('img', { name: /back/i })).toBeInTheDocument();

    await user.unhover(screen.getByTestId('row-brand_name'));
    expect(screen.getByRole('img', { name: /back/i })).toBeInTheDocument();
  });

  it('does not move the picture when a row is merely hovered', async () => {
    // Opening on the front, brushing the row whose outline is on the back must not turn
    // the page — and `mouseleave` names no region, so nothing would turn it back.
    const { user } = await reachChecked(twoPictures());

    await user.hover(screen.getByTestId('row-government_warning'));
    expect(screen.getByRole('img', { name: /front/i })).toBeInTheDocument();

    await user.unhover(screen.getByTestId('row-government_warning'));
    expect(screen.getByRole('img', { name: /front/i })).toBeInTheDocument();
  });

  it('turns to the right picture when a banner row link is clicked', async () => {
    // The deliberate path. The warning's outline is on the back; the screen opens on the
    // front, so following the link has to take the agent there or the row's own
    // "outlined as N on the picture" is a false statement.
    const { user } = await reachChecked(twoPictures());

    expect(screen.getByRole('img', { name: /front/i })).toBeInTheDocument();
    await user.click(
      within(screen.getByRole('region', { name: /recommendation/i })).getByRole('button', {
        name: /government warning/i,
      }),
    );

    expect(screen.getByRole('img', { name: /back/i })).toBeInTheDocument();
    expect(screen.getByTestId('region-government_warning')).toBeInTheDocument();
  });

  it('ignores a region naming a picture that does not exist', async () => {
    // `image_index` is the server's. Unclamped it selected an index with no URL behind
    // it: the panel fell back to "not available to display" and, with only one picture,
    // no switcher existed to undo it — the label was gone until the agent started over,
    // discarding every decision on the screen.
    const { user } = await reachChecked(
      samplePayload({
        fields: [
          fieldResult('government_warning', 'mismatch', {
            evidence: { image_index: 3, bbox: { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 } },
          }),
        ],
      }),
    );

    await user.click(
      within(screen.getByRole('region', { name: /recommendation/i })).getByRole('button', {
        name: /government warning/i,
      }),
    );

    expect(screen.getByRole('img', { name: /front/i })).toBeInTheDocument();
    expect(screen.queryByText(/not available to display/i)).not.toBeInTheDocument();
  });
});

describe('accessibility of the checked screen', () => {
  it('has no axe violations', async () => {
    // This is the audit that would have caught the h4-under-an-h2 in `FieldRow`. It sat
    // on this screen the whole time and nothing rendered it.
    const { view } = await reachChecked();
    const results = await axe(view.container);
    expect(
      results.violations.map((v) => `${v.id}: ${v.help}`),
    ).toEqual([]);
  });

  it('announces the outcome to a screen reader without needing the banner', async () => {
    await reachChecked();
    const status = screen
      .getAllByRole('status')
      .map((el) => el.textContent ?? '')
      .join(' ');
    expect(status).toMatch(/return for correction/i);
    expect(status).toMatch(/of 7 rows need review/i);
  });
});
