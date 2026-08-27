import '@testing-library/jest-dom/vitest';

// Recharts' ResponsiveContainer measures the DOM, which jsdom does not lay out.
// Stubbing the observer keeps chart-containing components renderable in tests.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
