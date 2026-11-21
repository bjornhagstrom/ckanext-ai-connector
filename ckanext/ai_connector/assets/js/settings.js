// AI settings page.
// - The model list fills the model field; "Other model…" shows the field
//   for a model that is not in the list. Without the script the field
//   alone is shown.
// - Choosing a model selects the key its service uses (gemini/... ->
//   GEMINI_API_KEY), when that key is there; choosing a key shows only its
//   service's models and suggests one.
// - In the purposes' rows the key follows the model (see the last part).
(function () {
  if (typeof document === "undefined") return;
  // Adding a key: the name field only for "Other service…" (without the script both are shown).
  var service = document.getElementById("custom_service");
  var nameGroup = document.getElementById("custom_name_group");
  if (service && nameGroup) {
    var showName = function () { nameGroup.hidden = service.value !== "other"; };
    service.addEventListener("change", showName);
    showName();
  }
})();

(function () {
  if (typeof document === "undefined") return;
  var model = document.getElementById("ai_model");
  var choice = document.getElementById("ai_model_choice");
  var key = document.getElementById("ai_api_key_env");
  if (!model) return;
  var keys = JSON.parse(model.getAttribute("data-provider-keys") || "{}");

  // The model list follows the chosen API key: only that service's models, and "Other model…".
  var groups = choice ? Array.prototype.slice.call(choice.querySelectorAll("optgroup")) : [];
  var otherOption = choice ? choice.querySelector('option[value=""]') : null;
  var recommended = choice ? JSON.parse(choice.getAttribute("data-recommended") || "{}") : {};
  var services = choice ? JSON.parse(choice.getAttribute("data-services") || "{}") : {};
  var hint = document.getElementById("ai_model_for_key");

  function providerOfKey(env) {
    for (var provider in keys) { if (keys[provider] === env) return provider; }
    return null;
  }

  function filterModels(env) {
    if (!choice) return;
    var provider = providerOfKey(env);
    var selected = choice.value;
    while (choice.firstChild) choice.removeChild(choice.firstChild);
    groups.forEach(function (group) {
      // No key chosen: every service. A key of one's own: no list, only "Other model…".
      if (!env || (provider && group.getAttribute("data-provider") === provider)) choice.appendChild(group);
    });
    if (otherOption) choice.appendChild(otherOption);
    choice.value = selected;
    if (choice.value !== selected && otherOption) choice.value = "";
  }

  // A new key chosen: suggest a model of its service and say what to do next.
  function followKey() {
    if (!key) return;
    filterModels(key.value);
    if (!choice || !key.value) return;
    var provider = providerOfKey(key.value);
    if (provider && model.value.split("/")[0] === provider) return;
    var pick = provider ? (recommended[provider] || "") : "";
    if (!pick && provider) {
      var first = choice.querySelector('optgroup[data-provider="' + provider + '"] option');
      if (first) pick = first.value;
    }
    choice.value = pick;
    model.value = pick;
    model.hidden = !!pick;
    say(pick ? hint.getAttribute("data-text").replace("{service}", services[provider] || provider)
             : hint.getAttribute("data-other"));
    if (!pick) model.focus();
  }

  function say(text) {
    if (!hint) return;
    hint.textContent = text;
    hint.hidden = !text;
    hint.style.display = text ? "block" : "none";
  }

  // A model written or chosen: its service's key, when there is one. When there is none, say so (the server refuses to
  // send another service's key with it anyway).
  function selectKey() {
    var provider = model.value.split("/")[0];
    var env = keys[provider];
    if (key && env) {
      if (key.querySelector('option[value="' + env + '"]')) {
        key.value = env;
        say("");
      } else if (hint) {
        say(hint.getAttribute("data-nokey").replace("{service}", services[provider] || provider));
      }
    }
    if (key) filterModels(key.value);
  }
  if (key) key.addEventListener("change", followKey);

  if (choice) {
    choice.hidden = false;
    if (key) filterModels(key.value);   // the list starts with the chosen key's service
    model.hidden = !!choice.value;
    choice.addEventListener("change", function () {
      if (choice.value) {
        model.value = choice.value;
        model.hidden = true;
        selectKey();
      } else {
        model.hidden = false;
        model.focus();
      }
    });
  }
  model.addEventListener("change", selectKey);
})();

// Models for different purposes: a row's key follows its model.
// - A model of a known service (anthropic/..., gemini/...) takes that service's key when it is the only key set that fits
//   it; with several (another name the server allows, api_key_envs) the sysadmin chooses.
// - A key the sysadmin chose and that fits the model stays. A key that does not fit (another known service's, or one added
//   under a name of its own for a known service's model) is let go: the row shows "the key of the model's service" again.
// - A key added under a name of its own is never chosen by the script: the page does not know which service it is for.
// - Only a key the script chose itself is let go when the model is emptied or has no service prefix (LiteLLM then decides
//   the service, and the server checks it on Save).
// - Nothing changes with an AI server of one's own (every key goes there).
// The choice itself is purposeKeyChoice (no page needed, tested with node --test); it returns {value, auto} or null.
function purposeKeyChoice(model, current, auto, options, providerKeys, onLoad) {
  model = (model || "").trim();
  var slash = model.indexOf("/");
  if (!model || slash <= 0) return auto && !onLoad ? { value: "", auto: false } : null;
  if (onLoad && current) return null;
  var service = model.slice(0, slash);
  var own = providerKeys[service] || "";
  function fits(o) {
    var otherName = !o.service && !o.custom;   // another name the server allows: goes where its model goes
    return own ? (o.value === own || otherName) : (o.custom || otherName);
  }
  var chosen = null;
  options.forEach(function (o) { if (o.value === current) chosen = o; });
  if (chosen && chosen.value && fits(chosen) && !auto) return null;
  var candidates = options.filter(function (o) { return o.value && o.set && fits(o); });
  if (own && candidates.length === 1 && candidates[0].value === own) {
    return current === own && auto ? null : { value: own, auto: true };
  }
  if (chosen && chosen.value && (auto || !fits(chosen))) return { value: "", auto: false };
  return null;
}

(function () {
  if (typeof document === "undefined") return;
  var form = document.getElementById("purposes-form");
  if (!form || form.getAttribute("data-api-base")) return;
  var providerKeys = JSON.parse(form.getAttribute("data-provider-keys") || "{}");

  function follow(input, onLoad) {
    var select = form.querySelector('select[name="' + input.getAttribute("data-purpose-key") + '"]');
    if (!select) return;
    var options = Array.prototype.map.call(select.options, function (o) {
      return { value: o.value, service: o.getAttribute("data-service") || "", custom: !!o.getAttribute("data-custom"),
               set: !!o.getAttribute("data-set") };
    });
    var choice = purposeKeyChoice(input.value, select.value, !!select.getAttribute("data-auto"), options, providerKeys,
                                  onLoad);
    if (!choice) return;
    select.value = choice.value;
    if (choice.auto) select.setAttribute("data-auto", "yes"); else select.removeAttribute("data-auto");
  }

  Array.prototype.forEach.call(form.querySelectorAll("input[data-purpose-key]"), function (input) {
    var select = form.querySelector('select[name="' + input.getAttribute("data-purpose-key") + '"]');
    if (select) select.addEventListener("change", function () { select.removeAttribute("data-auto"); });
    input.addEventListener("change", function () { follow(input, false); });
    input.addEventListener("input", function () { follow(input, false); });
    follow(input, true);
  });
})();

// "Changed, not saved" beside a purpose whose field differs from its saved model.
(function () {
  if (typeof document === "undefined") return;
  Array.prototype.forEach.call(document.querySelectorAll("small[data-purpose-unsaved]"), function (note) {
    var input = document.getElementById(note.getAttribute("data-purpose-unsaved"));
    if (!input) return;
    var update = function () { note.hidden = input.value.trim() === (input.getAttribute("data-saved") || ""); };
    input.addEventListener("input", update);
    input.addEventListener("change", update);
  });
})();

// The × beside a purpose's model: empties the field (the main model), which also makes the browser list every model again
// (it only shows those that match what is typed). The key follows, as when the field is emptied by hand.
(function () {
  if (typeof document === "undefined") return;
  Array.prototype.forEach.call(document.querySelectorAll("button[data-purpose-clear]"), function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.getAttribute("data-purpose-clear"));
      if (!input) return;
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    });
  });
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { purposeKeyChoice: purposeKeyChoice };
}
