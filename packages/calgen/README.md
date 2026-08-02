# calgen

A static site generator for local tech event calendars.

## Installation

```bash
pip install -e .
```

## Usage

```bash
calgen init          # create a new site
calgen refresh       # fetch iCal feeds
calgen pipeline      # generate _data/all_events.json
calgen build         # freeze to static HTML
calgen rebuild       # refresh + pipeline + build in one shot
calgen serve         # run dev server
```
