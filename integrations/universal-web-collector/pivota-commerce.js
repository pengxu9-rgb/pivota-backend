(function (window, document) {
  "use strict";

  var VERSION = "1.0.0";
  var ALLOWED_EVENTS = {
    "agent.requested": true,
    "search.performed": true,
    "product.viewed": true,
    "cart.created": true,
    "cart.item_added": true,
    "cart.item_removed": true,
    "cart.updated": true,
    "checkout.started": true,
    "checkout.submitted": true,
    "payment.attempted": true
  };
  var COPY_FIELDS = [
    "interaction_id", "agent_id", "caller_id", "prompt_id", "result_id",
    "click_id", "cart_id", "quote_id", "checkout_id", "payment_id",
    "canonical_product_id", "canonical_variant_id", "trace_id", "brief_id",
    "source_channel", "query_source", "protocol_name", "llm_provider",
    "llm_model", "surface"
  ];
  var CLICK_PATTERN = /^clk_[A-Za-z0-9_-]{6,60}$/;
  var previous = window.PivotaCommerce;
  var pendingCommands = previous && previous.q ? previous.q.slice() : [];
  var state = {
    token: "",
    endpoint: "",
    consent: "pending",
    queue: [],
    timer: null,
    flushing: false,
    context: {}
  };

  function storage(kind) {
    try {
      return window[kind];
    } catch (_error) {
      return null;
    }
  }

  function getStored(kind, key) {
    var target = storage(kind);
    try {
      return target ? target.getItem(key) : null;
    } catch (_error) {
      return null;
    }
  }

  function setStored(kind, key, value) {
    var target = storage(kind);
    try {
      if (target) target.setItem(key, value);
    } catch (_error) {
      // Storage can be blocked by privacy modes; in-memory collection still works.
    }
  }

  function removeStored(kind, key) {
    var target = storage(kind);
    try {
      if (target) target.removeItem(key);
    } catch (_error) {
      // Best effort only.
    }
  }

  function randomId(prefix) {
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < bytes.length; i += 1) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
    var value = "";
    for (var j = 0; j < bytes.length; j += 1) {
      value += bytes[j].toString(16).padStart(2, "0");
    }
    return prefix + value;
  }

  function tokenScope() {
    return state.token ? state.token.slice(-16).replace(/[^A-Za-z0-9_-]/g, "") : "none";
  }

  function keys() {
    var scope = tokenScope();
    return {
      visitor: "pivota:commerce:visitor:" + scope,
      session: "pivota:commerce:session:" + scope,
      context: "pivota:commerce:context:" + scope,
      queue: "pivota:commerce:queue:" + scope
    };
  }

  function parseJSON(value, fallback) {
    try {
      return value ? JSON.parse(value) : fallback;
    } catch (_error) {
      return fallback;
    }
  }

  function ensureIdentifiers() {
    if (state.consent !== "granted") return {};
    var scoped = keys();
    var visitor = getStored("localStorage", scoped.visitor) || randomId("vis_");
    var session = getStored("sessionStorage", scoped.session) || randomId("ses_");
    setStored("localStorage", scoped.visitor, visitor);
    setStored("sessionStorage", scoped.session, session);
    return { visitor_id: visitor, session_id: session };
  }

  function cleanText(value, maxLength) {
    if (value === null || value === undefined) return undefined;
    var text = String(value).trim();
    if (!text) return undefined;
    return text.slice(0, maxLength || 128);
  }

  function attributionFromLocation() {
    var params;
    try {
      params = new URLSearchParams(window.location.search || "");
    } catch (_error) {
      return {};
    }
    var click = params.get("pivota_click_id") || params.get("pvt_click_id");
    if (!click) {
      var utmContent = params.get("utm_content");
      if (utmContent && CLICK_PATTERN.test(utmContent)) click = utmContent;
    }
    var next = {};
    if (click && CLICK_PATTERN.test(click)) next.click_id = click;
    var mapping = {
      pivota_agent_id: "agent_id",
      pvt_agent_id: "agent_id",
      pivota_source_channel: "source_channel",
      pvt_source: "source_channel",
      pivota_protocol: "protocol_name",
      pivota_llm_provider: "llm_provider",
      pivota_llm_model: "llm_model",
      pivota_prompt_id: "prompt_id",
      pivota_result_id: "result_id",
      pivota_brief_id: "brief_id"
    };
    Object.keys(mapping).forEach(function (queryKey) {
      var value = cleanText(params.get(queryKey), 128);
      if (value) next[mapping[queryKey]] = value;
    });
    return next;
  }

  function persistContext() {
    if (state.consent === "granted") {
      setStored("sessionStorage", keys().context, JSON.stringify(state.context));
    }
  }

  function loadState() {
    if (state.consent !== "granted") return;
    var scoped = keys();
    var inMemoryContext = state.context;
    state.context = parseJSON(getStored("sessionStorage", scoped.context), {});
    Object.keys(inMemoryContext).forEach(function (key) {
      state.context[key] = inMemoryContext[key];
    });
    var locationContext = attributionFromLocation();
    Object.keys(locationContext).forEach(function (key) {
      state.context[key] = locationContext[key];
    });
    persistContext();
    var restored = parseJSON(getStored("sessionStorage", scoped.queue), []);
    state.queue = Array.isArray(restored) ? restored.slice(-100) : [];
  }

  function persistQueue() {
    if (state.consent === "granted") {
      setStored("sessionStorage", keys().queue, JSON.stringify(state.queue.slice(-100)));
    }
  }

  function safeMetadata(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
    var metadata = {};
    if (Number.isFinite(Number(value.quantity)) && Number(value.quantity) >= 0) {
      metadata.quantity = Math.min(Number(value.quantity), 1000000);
    }
    var topic = cleanText(value.native_topic, 128);
    if (topic) metadata.native_topic = topic;
    return Object.keys(metadata).length ? metadata : undefined;
  }

  function buildEvent(eventType, properties) {
    var input = properties && typeof properties === "object" ? properties : {};
    var identifiers = ensureIdentifiers();
    var event = {
      event_id: cleanText(input.event_id, 255) || randomId("web_"),
      event_type: eventType,
      occurred_at: cleanText(input.occurred_at, 64) || new Date().toISOString(),
      session_id: identifiers.session_id,
      visitor_id: identifiers.visitor_id
    };
    Object.keys(state.context).forEach(function (key) {
      if (state.context[key] !== undefined) event[key] = state.context[key];
    });
    COPY_FIELDS.forEach(function (field) {
      var value = cleanText(input[field], field === "interaction_id" ? 64 : 128);
      if (value) event[field] = value;
    });
    var metadata = safeMetadata(input.metadata || input);
    if (metadata) event.metadata = metadata;
    return event;
  }

  function scheduleFlush() {
    if (state.timer || state.consent !== "granted") return;
    state.timer = window.setTimeout(function () {
      state.timer = null;
      api.flush();
    }, 2000);
  }

  function requeue(entries) {
    entries.forEach(function (entry) {
      entry.attempts = (entry.attempts || 0) + 1;
      if (entry.attempts <= 5) state.queue.push(entry);
    });
    state.queue = state.queue.slice(-100);
    persistQueue();
    if (state.queue.length) scheduleFlush();
  }

  function payloadFor(entries) {
    return JSON.stringify({
      collector_token: state.token,
      events: entries.map(function (entry) { return entry.event; })
    });
  }

  function flushWithBeacon() {
    if (!window.navigator.sendBeacon || !state.queue.length || !state.endpoint) return false;
    var entries = state.queue.splice(0, 20);
    var body = new Blob([payloadFor(entries)], { type: "text/plain;charset=UTF-8" });
    if (window.navigator.sendBeacon(state.endpoint, body)) {
      persistQueue();
      return true;
    }
    state.queue = entries.concat(state.queue).slice(-100);
    persistQueue();
    return false;
  }

  var api = {
    version: VERSION,
    q: [],

    init: function (options) {
      var config = options || {};
      if (config.token) state.token = cleanText(config.token, 4096) || "";
      if (config.endpoint) state.endpoint = cleanText(config.endpoint, 1024) || "";
      if (config.consent) api.setConsent(config.consent);
      if (config.context && typeof config.context === "object") {
        Object.keys(config.context).forEach(function (key) {
          if (COPY_FIELDS.indexOf(key) >= 0) {
            var value = cleanText(config.context[key], 128);
            if (value) state.context[key] = value;
          }
        });
        persistContext();
      }
      if (state.consent === "granted") {
        ensureIdentifiers();
        if (state.queue.length) scheduleFlush();
      }
      return api;
    },

    setConsent: function (value) {
      var next = String(value || "").toLowerCase();
      if (["pending", "granted", "denied"].indexOf(next) < 0) {
        throw new Error("PivotaCommerce consent must be pending, granted, or denied");
      }
      state.consent = next;
      if (next === "granted") {
        loadState();
        ensureIdentifiers();
        scheduleFlush();
      } else if (next === "denied") {
        var scoped = keys();
        state.queue = [];
        state.context = {};
        removeStored("localStorage", scoped.visitor);
        removeStored("sessionStorage", scoped.session);
        removeStored("sessionStorage", scoped.context);
        removeStored("sessionStorage", scoped.queue);
      }
      return api;
    },

    setContext: function (context) {
      var input = context && typeof context === "object" ? context : {};
      COPY_FIELDS.forEach(function (field) {
        var value = cleanText(input[field], 128);
        if (value) state.context[field] = value;
      });
      persistContext();
      return api;
    },

    track: function (eventType, properties) {
      var normalized = String(eventType || "").trim().toLowerCase();
      if (!ALLOWED_EVENTS[normalized]) {
        throw new Error("PivotaCommerce does not allow browser event type: " + normalized);
      }
      if (state.consent !== "granted" || !state.token || !state.endpoint) return null;
      var event = buildEvent(normalized, properties);
      state.queue.push({ event: event, attempts: 0 });
      state.queue = state.queue.slice(-100);
      persistQueue();
      if (state.queue.length >= 20) api.flush(); else scheduleFlush();
      return event.event_id;
    },

    flush: function () {
      if (
        state.flushing || state.consent !== "granted" ||
        !state.queue.length || !state.token || !state.endpoint
      ) return Promise.resolve(false);
      state.flushing = true;
      var entries = state.queue.splice(0, 20);
      persistQueue();
      return window.fetch(state.endpoint, {
        method: "POST",
        mode: "cors",
        credentials: "omit",
        keepalive: true,
        headers: { "Content-Type": "text/plain;charset=UTF-8" },
        body: payloadFor(entries)
      }).then(function (response) {
        if (!response.ok) throw new Error("collector_http_" + response.status);
        return true;
      }).catch(function () {
        requeue(entries);
        return false;
      }).finally(function () {
        state.flushing = false;
        if (state.queue.length) scheduleFlush();
      });
    }
  };

  window.PivotaCommerce = api;
  var script = document.currentScript;
  if (script) {
    var scriptUrl = new URL(script.src, window.location.href);
    api.init({
      token: script.getAttribute("data-pivota-token") || "",
      endpoint: script.getAttribute("data-pivota-endpoint") ||
        scriptUrl.origin + "/merchant-events/v1/web/batch",
      consent: script.getAttribute("data-pivota-consent") || "pending"
    });
    if (
      script.getAttribute("data-pivota-auto-page") === "true" &&
      script.getAttribute("data-pivota-product-id")
    ) {
      api.track("product.viewed", {
        canonical_product_id: script.getAttribute("data-pivota-product-id"),
        canonical_variant_id: script.getAttribute("data-pivota-variant-id") || undefined
      });
    }
  }

  pendingCommands.forEach(function (command) {
    if (!Array.isArray(command) || !command.length) return;
    var method = command[0];
    if (typeof api[method] === "function") api[method].apply(api, command.slice(1));
  });

  window.addEventListener("online", function () { api.flush(); });
  window.addEventListener("pagehide", flushWithBeacon);
})(window, document);
