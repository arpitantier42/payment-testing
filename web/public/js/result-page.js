import { createApi, pollConfirmedStatus, readAccessToken, resolvePaymentId } from "./payment.js";

// Same tab as the pay page, so the session token is already in sessionStorage.
const { token } = readAccessToken({ hash: window.location.hash, storage: window.sessionStorage });
const api = createApi(window.fetch.bind(window), "", { getToken: () => token });
const params = new URLSearchParams(window.location.search);
const orderId = params.get("orderId");
// Gateway redirect parameters are a hint only; they never decide the shown result.
const gatewayHint = params.get("gateway_status") || params.get("status");
const paymentId = resolvePaymentId({
  storage: window.sessionStorage,
  orderId,
  queryPaymentId: params.get("paymentId"),
});

const els = {
  result: document.getElementById("result"),
  note: document.getElementById("note"),
  checkAgain: document.getElementById("check-again"),
  retry: document.getElementById("retry"),
};

const TEXT = {
  success: "Payment successful. Thank you!",
  pending: "Your payment is pending confirmation. We'll update this page once it is confirmed.",
};

function render(view) {
  els.result.className = `result result-${view.kind}`;
  els.result.dataset.testid = `result-${view.kind}`;
  els.result.textContent = TEXT[view.kind] || view.message;

  els.note.hidden = !view.overridden;
  if (view.overridden) {
    els.note.textContent = `The payment provider reported "${view.gatewayReported}", but the confirmed status is shown above.`;
  }
  els.checkAgain.hidden = !["pending", "network"].includes(view.kind);
  const canRetry = orderId && ["failed", "pending"].includes(view.kind);
  els.retry.hidden = !canRetry;
  if (canRetry) els.retry.href = `index.html?orderId=${encodeURIComponent(orderId)}`;
}

// The shown result is always GET /api/v1/payments/{paymentId}. While it is pending (e.g. the
// customer left checkout and the webhook has not arrived) it is re-checked a few times, then
// the page settles on Pending with a "Check again" button: it never spins indefinitely.
async function refresh() {
  els.checkAgain.disabled = true;
  await pollConfirmedStatus({ api, paymentId, gatewayHint, onUpdate: render });
  els.checkAgain.disabled = false;
}

els.checkAgain.addEventListener("click", refresh);
refresh();
