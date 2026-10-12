// GenMagic's section in ComfyUI's settings: where the API key is kept. ComfyUI saves it in
// the user's own settings file on this machine, and the nodes read it from there, so the
// key never goes into a workflow you share.
import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "GenMagic.Settings",
  settings: [
    {
      id: "GenMagic.APIKey",
      category: ["GenMagic", "Account", "API key"],
      name: "API key",
      type: "text",
      defaultValue: "",
      tooltip:
        "Your GenMagic API key (get one at genmagic.co/get-api-key). The GENMAGIC_API_KEY environment variable takes precedence when it is set.",
      attrs: { type: "password", autocomplete: "off", placeholder: "gm_..." },
    },
  ],
});
