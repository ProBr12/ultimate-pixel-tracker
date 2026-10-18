// Runs the hub's generated browser code in Node with fake browser and
// Shopify objects, and prints what it would send.
//   node browser_harness.js phone <phone.js> <cases.json>
//   node browser_harness.js pixel <pixel.js> <scenario.json>
//   node browser_harness.js theme <scripts.json> <scenario.json>
"use strict";
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");
const [mode, codePath, inputPath] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const out = (value) => process.stdout.write(JSON.stringify(value));

if (mode === "phone") {
  const ctx = vm.createContext({});
  vm.runInContext(fs.readFileSync(codePath, "utf8"), ctx);
  out(input.map(([phone, country]) => ctx.metaPhone(phone, country)));
} else if (mode === "pixel") {
  const subscribed = {};
  const fetches = [];
  const fbqCalls = [];
  const window = {};
  const document = {
    createElement: () => ({}),
    getElementsByTagName: () => [{ parentNode: { insertBefore: () => {} } }],
    head: { appendChild: () => {} },
  };
  const ctx = vm.createContext({
    window, document, navigator: { userAgent: "TestAgent/1.0" }, TextEncoder, crypto: crypto.webcrypto,
    console, Uint8Array, Array, JSON, Number, String, Promise,
    init: { data: { shop: { myshopifyDomain: input.shop } } },
    browser: {
      localStorage: { getItem: async (k) => (input.storage || {})[k] || null },
      cookie: { get: async (k) => (input.cookies || {})[k] || null },
    },
    analytics: { subscribe: (name, fn) => { (subscribed[name] = subscribed[name] || []).push(fn); } },
    fetch: async (url, options) => { fetches.push({ url, body: JSON.parse(options.body) }); return {}; },
  });
  vm.runInContext(fs.readFileSync(codePath, "utf8"), ctx);
  // The pixel's own fbq stub is replaced by one that records calls.
  window.fbq = (...args) => fbqCalls.push(args);
  (async () => {
    for (const fn of subscribed.checkout_completed || []) await fn({ data: { checkout: input.checkout } });
    await new Promise((r) => setTimeout(r, 20));
    out({ subscribed: Object.keys(subscribed).sort(), fetches, fbq: fbqCalls });
  })();
} else if (mode === "theme") {
  const scripts = JSON.parse(fs.readFileSync(codePath, "utf8"));
  const cookies = {};
  const store = Object.assign({}, input.localStorage || {});
  const fetches = [];
  const listeners = {};
  const location = new URL(input.url);
  const document = {
    get cookie() { return Object.entries(cookies).map(([k, v]) => k + "=" + v).join("; "); },
    set cookie(text) {
      const [pair, ...attrs] = text.split(";").map((s) => s.trim());
      const [name, ...rest] = pair.split("=");
      if (attrs.some((a) => a === "max-age=0")) delete cookies[name]; else cookies[name] = rest.join("=");
    },
    referrer: "",
    createElement: () => ({}),
    getElementsByTagName: () => [{ parentNode: { insertBefore: () => {} } }],
    addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
    getElementById: () => null,
  };
  const window = {
    location, document, routes: { cart_update_url: "/cart/update", cart_url: "/cart" },
    addEventListener: () => {},
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; },
    },
    fetch: async (url, options) => {
      fetches.push({ url, body: options && options.body });
      return { ok: true, json: async () => ({ attributes: {} }), clone() { return this; } };
    },
  };
  Object.assign(window, {
    window, console, URL, URLSearchParams, Promise, Date, Math, JSON, RegExp, Number, String, Object,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  });
  window.localStorage = window.localStorage;
  const ctx = vm.createContext(window);
  ctx.localStorage = window.localStorage;
  ctx.fetch = window.fetch;
  for (const code of scripts) vm.runInContext(code, ctx);
  const queue = (ctx.fbq && ctx.fbq.queue) || [];
  setTimeout(() => {
    out({
      fbq: queue.map((args) => Array.from(args)),
      track: fetches.filter((f) => String(f.url).endsWith("/track")).map((f) => JSON.parse(f.body)),
      restored: ctx.comfiRestored,
    });
  }, 20);
}
