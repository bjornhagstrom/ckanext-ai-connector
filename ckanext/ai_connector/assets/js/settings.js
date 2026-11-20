// AI settings page.
// - The model list fills the model field; "Other model…" shows the field
//   for a model that is not in the list. Without the script the field
//   alone is shown.
// - Choosing a model selects the key its service uses (gemini/... ->
//   GEMINI_API_KEY), when that key is there; choosing a key shows only its
//   service's models and suggests one.
(function () {
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
