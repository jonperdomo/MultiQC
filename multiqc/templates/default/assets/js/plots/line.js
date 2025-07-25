class LinePlot extends Plot {
  activeDatasetSize() {
    if (this.datasets.length === 0) return 0; // no datasets
    return this.datasets[this.activeDatasetIdx].lines.length; // no samples in a dataset
  }

  prepData(dataset, anonymize = false) {
    // Prepare data to either build Plotly traces or export as a file
    dataset = dataset ?? this.datasets[this.activeDatasetIdx];

    let lines = dataset.lines;

    let samples = lines.map((line) => line.name);
    let sampleSettings = applyToolboxSettings(samples);
    this.filtSampleSettings = sampleSettings.filter((s) => !s.hidden);

    lines = lines.filter((line, idx) => {
      if (sampleSettings[idx].name !== undefined) {
        line.name = anonymize ? sampleSettings[idx].pseudonym : sampleSettings[idx].name;
      }
      line.highlight = sampleSettings[idx].highlight;
      return !sampleSettings[idx].hidden;
    });

    return [samples, lines];
  }

  plotAiHeader() {
    let result = super.plotAiHeader();
    if (this.pconfig.xlab) result += `X axis: ${this.pconfig.xlab}\n`;
    if (this.pconfig.ylab) result += `Y axis: ${this.pconfig.ylab}\n`;
    return result;
  }

  formatDatasetForAiPrompt(dataset) {
    let [samples, lines] = this.prepData(dataset, true);

    // Check if all samples are hidden
    if (samples.length === 0) {
      return "All samples are hidden by user, so no data to analyse. Please inform user to use the toolbox to unhide samples.\n";
    }

    const xsuffix = this.layout.xaxis.ticksuffix;
    const ysuffix = this.layout.yaxis.ticksuffix;

    const formattedLines = lines.map((line) => {
      return {
        name: line.name,
        pairs: line.pairs.map((p) =>
          p.map((x, i) => {
            let val = !Number.isFinite(x) ? "" : Number.isInteger(x) ? x : parseFloat(x.toFixed(2));
            if (val !== "" && i === 0 && xsuffix) val += xsuffix;
            if (val !== "" && i === 1 && ysuffix) val += ysuffix;
            return val;
          }),
        ),
      };
    });

    return (
      "Samples: " +
      formattedLines.map((line) => line.name).join(", ") +
      "\n\n" +
      formattedLines
        .map((line) => {
          return line.name + " " + line.pairs.map((p) => "(" + p.join(": ") + ")").join(", ");
        })
        .join("\n\n")
    );
  }

  buildTraces() {
    let dataset = this.datasets[this.activeDatasetIdx];

    let [samples, lines] = this.prepData();
    if (lines.length === 0 || samples.length === 0) return [];

    // Reorder points so highlighted points are on top
    let highlighted = lines.filter((p) => p.highlight);
    let nonHighlighted = lines.filter((p) => !p.highlight);
    lines = nonHighlighted.concat(highlighted);

    return lines.map((line) => {
      let color = line.color;
      if (highlighted.length > 0) {
        color = line.highlight ?? "#cccccc";
      }

      let params = {
        line: {
          color: color,
          dash: line.dash,
          width: line.width,
        },
        marker: {
          color: color,
        },
        showlegend: line["showlegend"] ?? null,
        mode: line["mode"] ?? null,
      };

      let marker = line["marker"] ?? null;
      if (marker) {
        params.mode = "lines+markers";
        params.marker = {
          symbol: marker["symbol"],
          color: marker["fill_color"] ?? marker["color"] ?? color,
          line: {
            width: marker["width"],
            color: marker["line_color"] ?? marker["color"] ?? color,
          },
        };
      }

      updateObject(params, dataset["trace_params"], true);

      return {
        type: "scatter",
        x: line.pairs.map((x) => x[0]),
        y: line.pairs.map((x) => x[1]),
        name: line.name,
        text: line.pairs.map(() => line.name),
        ...params,
      };
    });
  }

  exportData(format) {
    let dataset = this.datasets[this.activeDatasetIdx];

    let [_, lines] = this.prepData();

    // check if all lines have the same x values
    let sharedX = true;
    let x = null;
    lines.forEach((line) => {
      let thisX;
      thisX = line.pairs.map((x) => x[0]);
      if (x === null) {
        x = thisX;
      } else if (x.length !== thisX.length) {
        sharedX = false;
      } else if (x.some((v, i) => v !== thisX[i])) {
        sharedX = false;
      }
    });

    let sep = format === "tsv" ? "\t" : ",";
    let csv = "";
    if (sharedX) {
      csv += "Sample" + sep + x.join(sep) + "\n";
      lines.forEach((line) => {
        csv += line.name + sep + line.pairs.map((x) => x[1]).join(sep) + "\n";
      });
    } else {
      lines.forEach((line) => {
        csv += line.name + sep + "X" + sep + line.pairs.map((x) => x[0]).join(sep) + "\n";
        csv += line.name + sep + "Y" + sep + line.pairs.map((x) => x[1]).join(sep) + "\n";
      });
    }
    return csv;
  }
}
