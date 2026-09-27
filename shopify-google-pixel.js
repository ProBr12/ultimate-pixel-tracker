// Install in Shopify Settings > Customer events as one custom pixel (no <script> tags).
// Require marketing permission and respect data-sale/sharing opt-outs in pixel settings.
// This associates a checkout with context; only the signed orders/paid webhook authorizes Purchase.
(function () {
  let privacy = init.customerPrivacy;
  let lastEvent = null;
  let lastVid = null;
  async function report(event) {
    try {
      if (!privacy || typeof privacy.marketingAllowed !== "boolean" || typeof privacy.saleOfDataAllowed !== "boolean") return;
      // Use Shopify's top-frame storage API, never sandbox window.localStorage.
      const vid = await browser.localStorage.getItem("comfi_vid");
      const stored = await browser.localStorage.getItem("comfi_google_attribution");
      lastVid = vid || lastVid;
      const allowed = privacy.marketingAllowed === true && privacy.saleOfDataAllowed === true;
      let attribution = null;
      try { attribution = allowed && stored ? JSON.parse(stored) : null; } catch (e) {}
      const checkout = event.data && event.data.checkout || {};
      await fetch("https://track.comfishop.com/google/context", {
        method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify({
          source: "pixel", event_id: event.id, vid: lastVid, client_id: event.clientId,
          checkout_token: checkout.token || null, order_id: checkout.order && checkout.order.id || null,
          attribution: attribution,
          consent: { marketingAllowed: privacy.marketingAllowed === true, saleOfDataAllowed: privacy.saleOfDataAllowed === true },
          consent_ts: Date.now()
        })
      });
    } catch (e) { /* Context reporting must not interrupt checkout. */ }
  }
  ["page_viewed", "checkout_started", "checkout_completed"].forEach(function (name) {
    analytics.subscribe(name, function (event) { lastEvent = event; return report(event); });
  });
  api.customerPrivacy.subscribe("visitorConsentCollected", async function (event) {
    privacy = event.customerPrivacy;
    if (privacy && (privacy.marketingAllowed === false || privacy.saleOfDataAllowed === false)) {
      try { await browser.localStorage.removeItem("comfi_google_attribution"); } catch (e) {}
    }
    if (lastEvent) report(lastEvent);
  });
})();
