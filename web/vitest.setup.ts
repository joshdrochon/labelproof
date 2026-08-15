import '@testing-library/jest-dom/vitest';

// jsdom implements no layout, so `Element.scrollIntoView` does not exist on it. Both
// screens call it after a deliberate jump to a field, so without this every such test
// throws an unhandled `TypeError` *after* the assertion it was checking has already
// passed — vitest reports the file green and warns about the exception separately, which
// is the shape of a suite that is measuring less than it appears to.
//
// Stubbed rather than guarded in the product: every browser implements it, and an
// optional call there would be dead defensiveness written to satisfy a test harness.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
