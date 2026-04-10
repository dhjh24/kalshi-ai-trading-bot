"use client";

import dynamic from "next/dynamic";

const ReactECharts = dynamic(() => import("echarts-for-react"), { ssr: false });

export function LineChart({
  title,
  data,
  color = "#0f766e",
  yAxisLabel = "Value"
}: {
  title: string;
  data: Array<{ timestamp: string; value: number }>;
  color?: string;
  yAxisLabel?: string;
}) {
  return (
    <ReactECharts
      style={{ height: 320 }}
      option={{
        backgroundColor: "transparent",
        title: { text: title, textStyle: { color: "#1f2937", fontSize: 16 } },
        tooltip: { trigger: "axis" },
        grid: { left: 36, right: 20, top: 48, bottom: 36 },
        xAxis: {
          type: "category",
          data: data.map((point) => new Date(point.timestamp).toLocaleTimeString()),
          boundaryGap: false,
          axisLine: { lineStyle: { color: "#cbd5e1" } }
        },
        yAxis: {
          type: "value",
          name: yAxisLabel,
          axisLine: { lineStyle: { color: "#cbd5e1" } },
          splitLine: { lineStyle: { color: "#e2e8f0" } }
        },
        series: [
          {
            type: "line",
            smooth: true,
            showSymbol: false,
            lineStyle: { color, width: 3 },
            areaStyle: { color: `${color}22` },
            data: data.map((point) => point.value)
          }
        ]
      }}
    />
  );
}

export function CandlestickChart({
  title,
  candles
}: {
  title: string;
  candles: Array<[number, number, number, number, number]>;
}) {
  return (
    <ReactECharts
      style={{ height: 360 }}
      option={{
        backgroundColor: "transparent",
        title: { text: title, textStyle: { color: "#1f2937", fontSize: 16 } },
        tooltip: { trigger: "axis" },
        grid: { left: 36, right: 20, top: 48, bottom: 36 },
        xAxis: {
          type: "category",
          data: candles.map((entry) => new Date(entry[0]).toLocaleTimeString()),
          boundaryGap: true,
          axisLine: { lineStyle: { color: "#cbd5e1" } }
        },
        yAxis: {
          scale: true,
          axisLine: { lineStyle: { color: "#cbd5e1" } },
          splitLine: { lineStyle: { color: "#e2e8f0" } }
        },
        series: [
          {
            type: "candlestick",
            data: candles.map((entry) => [entry[1], entry[4], entry[3], entry[2]]),
            itemStyle: {
              color: "#0f766e",
              color0: "#be123c",
              borderColor: "#0f766e",
              borderColor0: "#be123c"
            }
          }
        ]
      }}
    />
  );
}
