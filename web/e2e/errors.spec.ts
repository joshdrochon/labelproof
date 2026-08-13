/**
 * LP-316 — every error state visited, in a real browser, against the deployed app (UX-6).
 *
 * The ticket said "by hand". This is the same sweep automated, which is better in one way
 * and worse in another, and both are worth stating. Better: it runs on every change, in
 * four engines, and cannot get bored on the fourteenth case. Worse: it asserts that a
 * sentence is present and actionable by SHAPE — no stack trace, no jargon, a way forward —
 * and it cannot tell you whether the sentence is the RIGHT one for a person in a hurry.
 * That judgement is what LP-319's fresh-eyes pass is for, and it is still open.
 *
 * The asymmetry rule applies to error copy too: a message that leaves an agent unsure
 * whether their label was checked is worse than an ugly one that is clear.
 */

import { expect, test, type Page } from '@playwright/test';

/** Every error message must be a sentence an agent can act on (UX-6, OPS-5). */
async function assertActionable(page: Page, where: string) {
  const text = (await page.locator('[role="alert"], .problem, .field__problem').allInnerTexts())
    .join(' ')
    .trim();

  expect(text, `${where}: nothing was said at all`).not.toBe('');
  // The taxonomy exists so that none of this ever reaches an agent.
  for (const leak of ['Traceback', 'Exception', 'undefined', 'null', '[object', 'Error:']) {
    expect(text, `${where}: leaked "${leak}"`).not.toContain(leak);
  }
  // Jargon the agents' own vocabulary does not contain.
  for (const jargon of ['inference', 'API', 'payload', 'JSON', '500', 'stack']) {
    expect(text.toLowerCase(), `${where}: used "${jargon}"`).not.toContain(jargon.toLowerCase());
  }
  expect(text.length, `${where}: message too short to be actionable`).toBeGreaterThan(20);
}

const FILLED = {
  brand_name: 'OLD TOM DISTILLERY',
  class_type: 'Kentucky Straight Bourbon Whiskey',
  net_contents: '750 mL',
  producer_name: 'Old Tom Distillery',
  producer_address: 'Bardstown, Kentucky',
};

async function fill(page: Page, overrides: Record<string, string> = {}) {
  for (const [id, value] of Object.entries({ ...FILLED, ...overrides })) {
    await page.locator(`#${id}`).fill(value);
  }
}

test.describe('LP-316 — the empty and the malformed', () => {
  test('submitting an empty form names every missing box', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: /check this label/i }).click();

    await assertActionable(page, 'empty form');
    // Naming the boxes is the point. One global "form invalid" would fail UX-6.
    await expect(page.locator('[aria-invalid="true"]')).not.toHaveCount(0);
  });

  test('an image with no application still says which fields are missing', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: /check this label/i }).click();

    const text = (await page.locator('.field__problem').allInnerTexts()).join(' ');
    expect(text).toContain('brand name');
  });

  test('two alcohol values are refused rather than guessed', async ({ page }) => {
    await page.goto('/');
    await fill(page, { alcohol_content: '45% (Front) / 43% (Back)' });
    await page.getByRole('button', { name: /check this label/i }).click();

    const problem = await page.locator('#alcohol_content-problem').innerText();
    // It must name BOTH numbers. "Enter a number" would be false — they did.
    expect(problem).toContain('43');
    expect(problem).toContain('45');
    await assertActionable(page, 'ambiguous alcohol');
  });

  test('two net-contents sizes are refused', async ({ page }) => {
    await page.goto('/');
    await fill(page, { net_contents: '750 mL / 700 mL' });
    await page.getByRole('button', { name: /check this label/i }).click();

    await expect(page.locator('#net_contents-problem')).toBeVisible();
    await assertActionable(page, 'ambiguous net contents');
  });

  test('a decorated alcohol entry is NOT refused', async ({ page }) => {
    // The other half of the rule. A gate that refuses good input teaches people to
    // work around it, and `alc. 45% by vol.` is what an agent pastes out of COLA.
    await page.goto('/');
    await fill(page, { alcohol_content: 'alc. 45% by vol.' });
    await page.getByRole('button', { name: /check this label/i }).click();

    // The empty slot deliberately carries no id — `aria-describedby` must never point
    // at a node with nothing in it, or a screen reader announces silence. So the claim
    // is about the field, not about finding an element that should not exist.
    await expect(page.locator('#alcohol_content')).not.toHaveAttribute('aria-invalid', 'true');
  });

  test('an import with no country of origin is caught', async ({ page }) => {
    await page.goto('/');
    await fill(page);
    await page.getByLabel(/imported/i).check();
    await page.getByRole('button', { name: /check this label/i }).click();

    await expect(page.locator('#country_of_origin-problem')).toBeVisible();
  });
});

test.describe('LP-316 — bad uploads', () => {
  test('a file that is not an image is refused by content, not by name', async ({ page }) => {
    await page.goto('/');
    await page.setInputFiles('input[type=file]', {
      name: 'label.png', // lies about itself
      mimeType: 'image/png',
      buffer: Buffer.from('this is plain text pretending to be a PNG'),
    });
    await fill(page);
    await page.getByRole('button', { name: /check this label/i }).click();

    await expect(page.locator('.problem, [role="alert"]').first()).toBeVisible({ timeout: 30_000 });
    await assertActionable(page, 'disguised text file');
    // Nothing may be reported as verified.
    await expect(page.locator('.checklist')).toHaveCount(0);
  });

  test('a refusal offers a way forward, not just a complaint', async ({ page }) => {
    await page.goto('/');
    await page.setInputFiles('input[type=file]', {
      name: 'x.png',
      mimeType: 'image/png',
      buffer: Buffer.from('not an image'),
    });
    await fill(page);
    await page.getByRole('button', { name: /check this label/i }).click();
    await expect(page.locator('.problem, [role="alert"]').first()).toBeVisible({ timeout: 30_000 });

    // UX-6: every dead end has a door. `next_step` drives what the UI offers.
    const actions = await page.locator('.problem button, [role="alert"] button').allInnerTexts();
    expect(actions.length, 'a refusal with no way forward').toBeGreaterThan(0);
  });
});

test.describe('LP-316 — the API taxonomy reaches the browser intact', () => {
  test('an unknown address answers in the taxonomy, not with a stack trace', async ({ page }) => {
    // `/sample` is a real API prefix; `/api` is not one this service uses, and an
    // unknown path under it correctly falls through to the app shell.
    const response = await page.request.get('/sample/nope');
    expect(response.status()).toBe(404);
    const body = await response.json();

    expect(body.error).toMatchObject({ kind: expect.any(String), code: expect.any(String) });
    expect(body.error.message.length).toBeGreaterThan(20);
    expect(JSON.stringify(body)).not.toContain('Traceback');
  });

  test('an unknown client route still loads the app rather than 404ing', async ({ page }) => {
    // A deep link an agent pasted from a colleague must not dead-end.
    await page.goto('/some/deep/link');
    await expect(page.locator('main')).toBeVisible();
  });

  test('a malformed application is a 400 with a sentence', async ({ page }) => {
    const response = await page.request.post('/verify', {
      multipart: {
        application: 'not json at all',
        images: { name: 'a.png', mimeType: 'image/png', buffer: Buffer.from('x') },
      },
    });

    expect(response.status()).toBe(400);
    const body = await response.json();
    expect(body.error.message).toContain('application');
    expect(body.error.next_step).toBeTruthy();
  });
});
