# Additional CSS Themes

This folder contains other examples of customized CSS for webpublish sites. There are some built-in color schemes
with the main plugin, but customization is virtually endless by importing your own CSS.

Themes can be hosted statically and imported in the config file like:

```
  css: |
      @import url("https://path.to.your/theme.css")

```

Or you could copy-and-paste the whole thing into the config.

Themes can also be set per-room with the `!webpublish config css <your css here>` command.

Have fun!
