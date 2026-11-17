// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

#![allow(clippy::collapsible_if)]
#![forbid(unsafe_code)] // No `unsafe`, enforced by the compiler.

use std::env;
use std::io::Write;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::StreamExt;

use reqwest::Client;
use serde::Serialize;
use serde_json::{Value, json};
use tokio::sync::Semaphore;

#[derive(Debug, Clone, Serialize)]
struct BenchmarkResult {
    target: String,
    concurrency: usize,
    ttfb_ms: f64,
    e2e_ms: f64,
    completion_tokens: i64,
    tokens_per_sec: f64,
    status_code: u16,
    error: Option<String>,
}

#[derive(Debug, Clone)]
struct BenchmarkConfig {
    openai_key: Option<String>,
    anthropic_key: Option<String>,
    proxy_url: Option<String>,
    proxy_api_key: Option<String>,
    /// Model sent to OpenAI directly (and through the gateway unless
    /// `proxy_openai_model` overrides it).
    openai_model: String,
    anthropic_model: String,
    /// The name the gateway knows the same model by, when it differs.
    proxy_openai_model: Option<String>,
    proxy_anthropic_model: Option<String>,
    concurrency_levels: Vec<usize>,
    requests_per_level: usize,
    output_dir: String,
}

impl BenchmarkConfig {
    fn from_env() -> Self {
        Self {
            openai_key: env::var("BENCHMARK_OPENAI_KEY").ok(),
            anthropic_key: env::var("BENCHMARK_ANTHROPIC_KEY").ok(),
            proxy_url: env::var("BENCHMARK_PROXY_URL").ok(),
            proxy_api_key: env::var("BENCHMARK_PROXY_API_KEY").ok(),
            openai_model: env::var("BENCHMARK_OPENAI_MODEL")
                .unwrap_or_else(|_| "gpt-6-luna".into()),
            anthropic_model: env::var("BENCHMARK_ANTHROPIC_MODEL")
                .unwrap_or_else(|_| "claude-haiku-5-5".into()),
            proxy_openai_model: env::var("BENCHMARK_PROXY_OPENAI_MODEL").ok(),
            proxy_anthropic_model: env::var("BENCHMARK_PROXY_ANTHROPIC_MODEL").ok(),
            concurrency_levels: env::var("BENCHMARK_CONCURRENCY")
                .unwrap_or_else(|_| "1,5,10".into())
                .split(',')
                .filter_map(|s| s.trim().parse().ok())
                .collect(),
            requests_per_level: env::var("BENCHMARK_REQUESTS")
                .unwrap_or_else(|_| "10".into())
                .parse()
                .unwrap_or(10),
            output_dir: env::var("BENCHMARK_OUTPUT_DIR")
                .unwrap_or_else(|_| "./benchmark-results".into()),
        }
    }
}

fn openai_request_body(model: &str) -> Value {
    json!({
        "model": model,
        "messages": [
            {"role": "user", "content": "Explain what a proxy server is in exactly 3 sentences. Be concise."}
        ],
        "max_completion_tokens": 100,
        "stream": true,
        "stream_options": {"include_usage": true}
    })
}

fn anthropic_request_body(model: &str) -> Value {
    json!({
        "model": model,
        "messages": [
            {"role": "user", "content": "Explain what a proxy server is in exactly 3 sentences. Be concise."}
        ],
        "max_tokens": 100,
        "temperature": 0.0,
        "stream": true
    })
}

/// Incremental SSE reader: lines can be split across network chunks (and a
/// multi-byte character across two), so bytes are buffered until a newline.
#[derive(Debug, Default)]
struct SseReader {
    buf: Vec<u8>,
    /// Time of the first `data:` event (time to first byte of the answer).
    ttfb: Option<Duration>,
    completion_tokens: i64,
    chunk_count: i64,
}

impl SseReader {
    /// Feed one network chunk. Returns true once `[DONE]` is seen.
    fn feed(&mut self, bytes: &[u8], at: Duration) -> bool {
        self.buf.extend_from_slice(bytes);
        while let Some(pos) = self.buf.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buf.drain(..=pos).collect();
            if self.line(String::from_utf8_lossy(&line).trim_end(), at) {
                return true;
            }
        }
        false
    }

    /// A final line with no trailing newline.
    fn finish(&mut self, at: Duration) {
        if !self.buf.is_empty() {
            let line = String::from_utf8_lossy(&std::mem::take(&mut self.buf)).into_owned();
            self.line(line.trim_end(), at);
        }
    }

    fn line(&mut self, line: &str, at: Duration) -> bool {
        let Some(data) = line.strip_prefix("data:") else {
            return false;
        };
        let data = data.trim_start();
        if self.ttfb.is_none() {
            self.ttfb = Some(at);
        }
        if data.trim() == "[DONE]" {
            return true;
        }
        self.chunk_count += 1;
        if let Ok(chunk) = serde_json::from_str::<Value>(data) {
            // OpenAI: a final chunk with `usage.completion_tokens`.
            // Anthropic: `usage.output_tokens` on message_delta.
            if let Some(usage) = chunk.get("usage") {
                if let Some(ct) = usage.get("completion_tokens").and_then(|v| v.as_i64()) {
                    self.completion_tokens = ct;
                }
                if let Some(ot) = usage.get("output_tokens").and_then(|v| v.as_i64()) {
                    self.completion_tokens = ot;
                }
            }
        }
        false
    }
}

/// Send a streaming request and measure TTFB, e2e, tokens.
async fn measure_streaming_request(
    client: &Client,
    url: &str,
    headers: Vec<(String, String)>,
    body: &Value,
    target: &str,
) -> BenchmarkResult {
    let start = Instant::now();

    let mut req = client
        .post(url)
        .header("Content-Type", "application/json")
        .timeout(Duration::from_secs(60));

    for (k, v) in &headers {
        req = req.header(k.as_str(), v.as_str());
    }

    let resp = match req.json(body).send().await {
        Ok(r) => r,
        Err(e) => {
            return BenchmarkResult {
                target: target.into(),
                concurrency: 0,
                ttfb_ms: 0.0,
                e2e_ms: start.elapsed().as_secs_f64() * 1000.0,
                completion_tokens: 0,
                tokens_per_sec: 0.0,
                status_code: 0,
                error: Some(e.to_string()),
            };
        }
    };

    let status_code = resp.status().as_u16();
    if status_code >= 400 {
        let body_text = resp.text().await.unwrap_or_default();
        return BenchmarkResult {
            target: target.into(),
            concurrency: 0,
            ttfb_ms: start.elapsed().as_secs_f64() * 1000.0,
            e2e_ms: start.elapsed().as_secs_f64() * 1000.0,
            completion_tokens: 0,
            tokens_per_sec: 0.0,
            status_code,
            error: Some(body_text.chars().take(200).collect()),
        };
    }

    // Read the stream as it arrives. TTFB is when the first SSE event
    // lands; reading the whole body first (as this used to) made TTFB equal
    // to end-to-end time and the throughput figure meaningless.
    let mut stream = resp.bytes_stream();
    let mut sse = SseReader::default();
    while let Some(item) = stream.next().await {
        match item {
            Ok(bytes) => {
                if sse.feed(&bytes, start.elapsed()) {
                    break;
                }
            }
            Err(e) => {
                return BenchmarkResult {
                    target: target.into(),
                    concurrency: 0,
                    ttfb_ms: sse.ttfb.map_or(0.0, |t| t.as_secs_f64() * 1000.0),
                    e2e_ms: start.elapsed().as_secs_f64() * 1000.0,
                    completion_tokens: 0,
                    tokens_per_sec: 0.0,
                    status_code,
                    error: Some(format!("stream broke: {e}")),
                };
            }
        }
    }
    sse.finish(start.elapsed());

    let e2e = start.elapsed();
    let ttfb = sse.ttfb.unwrap_or(e2e);
    let ttfb_ms = ttfb.as_secs_f64() * 1000.0;

    // If we didn't get usage, estimate from chunk count (rough: 1 token per delta chunk)
    let completion_tokens = if sse.completion_tokens > 0 {
        sse.completion_tokens
    } else {
        sse.chunk_count
    };

    // Throughput over the generation, after the first event.
    let stream_duration_secs = (e2e - ttfb).as_secs_f64();
    let tokens_per_sec = if stream_duration_secs > 0.0 {
        completion_tokens as f64 / stream_duration_secs
    } else {
        0.0
    };

    BenchmarkResult {
        target: target.into(),
        concurrency: 0, // filled in by caller
        ttfb_ms,
        e2e_ms: e2e.as_secs_f64() * 1000.0,
        completion_tokens,
        tokens_per_sec,
        status_code,
        error: None,
    }
}

async fn run_concurrent_benchmark(
    client: &Client,
    url: &str,
    headers: Vec<(String, String)>,
    body: &Value,
    target: &str,
    concurrency: usize,
    num_requests: usize,
) -> Vec<BenchmarkResult> {
    let semaphore = Arc::new(Semaphore::new(concurrency));
    let mut handles = Vec::new();

    for _ in 0..num_requests {
        let client = client.clone();
        let url = url.to_string();
        let headers = headers.clone();
        let body = body.clone();
        let target = target.to_string();
        let sem = semaphore.clone();

        handles.push(tokio::spawn(async move {
            let _permit = sem.acquire().await.unwrap();
            let mut result =
                measure_streaming_request(&client, &url, headers, &body, &target).await;
            result.concurrency = concurrency;
            result
        }));
    }

    let mut results = Vec::new();
    for handle in handles {
        if let Ok(result) = handle.await {
            results.push(result);
        }
    }
    results
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64 - 1.0) * p / 100.0).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

#[derive(Serialize)]
struct SummaryRow {
    target: String,
    concurrency: usize,
    requests: usize,
    p50_ms: f64,
    p95_ms: f64,
    p99_ms: f64,
    avg_ttfb_ms: f64,
    avg_tokens_per_sec: f64,
    errors: usize,
}

fn compute_summary(results: &[BenchmarkResult], target: &str, concurrency: usize) -> SummaryRow {
    let filtered: Vec<&BenchmarkResult> = results
        .iter()
        .filter(|r| r.target == target && r.concurrency == concurrency && r.error.is_none())
        .collect();

    let errors = results
        .iter()
        .filter(|r| r.target == target && r.concurrency == concurrency && r.error.is_some())
        .count();

    let mut latencies: Vec<f64> = filtered.iter().map(|r| r.e2e_ms).collect();
    latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let avg_ttfb = if filtered.is_empty() {
        0.0
    } else {
        filtered.iter().map(|r| r.ttfb_ms).sum::<f64>() / filtered.len() as f64
    };
    let avg_tps = if filtered.is_empty() {
        0.0
    } else {
        filtered.iter().map(|r| r.tokens_per_sec).sum::<f64>() / filtered.len() as f64
    };

    SummaryRow {
        target: target.into(),
        concurrency,
        requests: filtered.len() + errors,
        p50_ms: percentile(&latencies, 50.0),
        p95_ms: percentile(&latencies, 95.0),
        p99_ms: percentile(&latencies, 99.0),
        avg_ttfb_ms: avg_ttfb,
        avg_tokens_per_sec: avg_tps,
        errors,
    }
}

/// Every request failed with a status no retry or concurrency level changes.
fn refused_outright(results: &[BenchmarkResult]) -> bool {
    !results.is_empty()
        && results
            .iter()
            .all(|r| r.error.is_some() && matches!(r.status_code, 401 | 403 | 404))
}

fn has_successes(row: &SummaryRow) -> bool {
    row.requests > row.errors
}

fn format_summary_table(rows: &[SummaryRow]) -> String {
    let mut out = String::new();
    out.push_str("# Brutor Agent Control Core Gateway Benchmark Results\n\n");
    out.push_str("_Methodology: LLMPerf (streaming chat completion, fixed prompt)_\n\n");
    out.push_str("| Target | Concurrency | Requests | P50 (ms) | P95 (ms) | P99 (ms) | Avg TTFB (ms) | Avg tok/s | Errors |\n");
    out.push_str("|--------|-------------|----------|----------|----------|----------|---------------|-----------|--------|\n");
    for r in rows {
        out.push_str(&format!(
            "| {} | {} | {} | {:.1} | {:.1} | {:.1} | {:.1} | {:.1} | {} |\n",
            r.target,
            r.concurrency,
            r.requests,
            r.p50_ms,
            r.p95_ms,
            r.p99_ms,
            r.avg_ttfb_ms,
            r.avg_tokens_per_sec,
            r.errors,
        ));
    }

    // Add overhead comparison if we have both direct and proxy results
    out.push_str("\n## Gateway Overhead\n\n");
    out.push_str("| Path | Concurrency | P50 Overhead | P95 Overhead | TTFB Overhead |\n");
    out.push_str("|------|-------------|--------------|--------------|---------------|\n");

    // Compare each proxy path against the corresponding direct path
    let comparisons = [
        ("openai-direct", "proxy-openai", "OpenAI (translation)"),
        (
            "anthropic-direct",
            "proxy-anthropic",
            "Anthropic (OpenAI translation)",
        ),
        (
            "anthropic-direct",
            "proxy-anthropic-native",
            "Anthropic (native pipeline)",
        ),
        (
            "anthropic-direct",
            "proxy-claude-code",
            "Claude Code (native + beta headers)",
        ),
    ];

    for (direct_target, proxy_target, label) in comparisons {
        for direct in rows.iter().filter(|r| r.target == direct_target) {
            if let Some(proxied) = rows
                .iter()
                .find(|r| r.target == proxy_target && r.concurrency == direct.concurrency)
            {
                // An overhead needs a measurement on both sides: a level where
                // every request on either side failed has nothing to compare.
                if !has_successes(direct) || !has_successes(proxied) {
                    out.push_str(&format!(
                        "| {} | {} | n/a | n/a | n/a |\n",
                        label, direct.concurrency
                    ));
                    continue;
                }
                let p50_overhead = proxied.p50_ms - direct.p50_ms;
                let p95_overhead = proxied.p95_ms - direct.p95_ms;
                let ttfb_overhead = proxied.avg_ttfb_ms - direct.avg_ttfb_ms;
                out.push_str(&format!(
                    "| {} | {} | {}{:.1}ms | {}{:.1}ms | {}{:.1}ms |\n",
                    label,
                    direct.concurrency,
                    if p50_overhead >= 0.0 { "+" } else { "" },
                    p50_overhead,
                    if p95_overhead >= 0.0 { "+" } else { "" },
                    p95_overhead,
                    if ttfb_overhead >= 0.0 { "+" } else { "" },
                    ttfb_overhead,
                ));
            }
        }
    }

    out
}

#[tokio::main]
async fn main() {
    // Load .env file if present (same as the main proxy binary)
    dotenvy::dotenv().ok();

    let config = BenchmarkConfig::from_env();

    println!("Brutor Agent Control Core Gateway Benchmark");
    println!("========================");
    println!();

    if config.openai_key.is_none() && config.anthropic_key.is_none() {
        eprintln!("Error: Set at least one of BENCHMARK_OPENAI_KEY or BENCHMARK_ANTHROPIC_KEY");
        std::process::exit(1);
    }

    println!("Configuration:");
    println!(
        "  OpenAI key:    {}",
        if config.openai_key.is_some() {
            "set"
        } else {
            "not set (skipping)"
        }
    );
    println!(
        "  Anthropic key: {}",
        if config.anthropic_key.is_some() {
            "set"
        } else {
            "not set (skipping)"
        }
    );
    println!(
        "  Gateway URL:   {}",
        config
            .proxy_url
            .as_deref()
            .unwrap_or("not set (skipping gateway tests)")
    );
    if config.proxy_url.is_some() {
        println!(
            "  Gateway API key: {}",
            if config.proxy_api_key.is_some() {
                "set"
            } else {
                "NOT SET: the gateway will refuse every proxied call (set BENCHMARK_PROXY_API_KEY)"
            }
        );
    }
    println!(
        "  Models:        {} / {}",
        config.openai_model, config.anthropic_model
    );
    if config.proxy_openai_model.is_some() || config.proxy_anthropic_model.is_some() {
        println!(
            "  Gateway models: {} / {}",
            config
                .proxy_openai_model
                .as_deref()
                .unwrap_or(&config.openai_model),
            config
                .proxy_anthropic_model
                .as_deref()
                .unwrap_or(&config.anthropic_model)
        );
    }
    println!("  Concurrency:   {:?}", config.concurrency_levels);
    println!("  Requests/level: {}", config.requests_per_level);
    println!("  Output:        {}", config.output_dir);
    println!();

    let client = Client::builder()
        .pool_max_idle_per_host(50)
        .build()
        .expect("Failed to create HTTP client");

    let mut all_results: Vec<BenchmarkResult> = Vec::new();

    // Build test targets
    struct Target {
        name: String,
        url: String,
        headers: Vec<(String, String)>,
        body: Value,
    }

    let mut targets: Vec<Target> = Vec::new();

    // OpenAI direct
    if let Some(ref key) = config.openai_key {
        targets.push(Target {
            name: "openai-direct".into(),
            url: "https://api.openai.com/v1/chat/completions".into(),
            headers: vec![("Authorization".into(), format!("Bearer {key}"))],
            body: openai_request_body(&config.openai_model),
        });
    }

    // Anthropic direct
    if let Some(ref key) = config.anthropic_key {
        targets.push(Target {
            name: "anthropic-direct".into(),
            url: "https://api.anthropic.com/v1/messages".into(),
            headers: vec![
                ("x-api-key".into(), key.clone()),
                ("anthropic-version".into(), "2023-06-01".into()),
            ],
            body: anthropic_request_body(&config.anthropic_model),
        });
    }

    // Proxy targets (OpenAI format through proxy)
    if let Some(ref proxy_url) = config.proxy_url {
        let proxy_base = proxy_url.trim_end_matches('/');
        let proxy_openai_model = config
            .proxy_openai_model
            .as_deref()
            .unwrap_or(&config.openai_model);
        let proxy_anthropic_model = config
            .proxy_anthropic_model
            .as_deref()
            .unwrap_or(&config.anthropic_model);

        // Proxy headers
        let mut proxy_headers: Vec<(String, String)> = Vec::new();
        if let Some(ref api_key) = config.proxy_api_key {
            proxy_headers.push(("Authorization".into(), format!("Bearer {api_key}")));
        }

        if config.openai_key.is_some() {
            targets.push(Target {
                name: "proxy-openai".into(),
                url: format!("{proxy_base}/v1/proxy/llm/chat/completions"),
                headers: proxy_headers.clone(),
                body: openai_request_body(proxy_openai_model),
            });
        }

        if config.anthropic_key.is_some() {
            // Through proxy, use OpenAI format with anthropic model (translation path)
            let body = openai_request_body(proxy_anthropic_model);
            targets.push(Target {
                name: "proxy-anthropic".into(),
                url: format!("{proxy_base}/v1/proxy/llm/chat/completions"),
                headers: proxy_headers.clone(),
                body,
            });

            // Through proxy, native Anthropic Messages API (native pipeline)
            // This is the path Claude Code uses via ANTHROPIC_BASE_URL
            let mut native_headers = Vec::new();
            if let Some(ref api_key) = config.proxy_api_key {
                native_headers.push(("X-API-Key".into(), api_key.clone()));
            }
            native_headers.push(("anthropic-version".into(), "2023-06-01".into()));

            targets.push(Target {
                name: "proxy-anthropic-native".into(),
                url: format!("{proxy_base}/v1/messages"),
                headers: native_headers.clone(),
                body: anthropic_request_body(proxy_anthropic_model),
            });

            // Simulate Claude Code client (with beta headers + UA).
            // Only send the claude-code identification flag — experimental
            // beta flags (interleaved-thinking, extended-thinking, etc.) may
            // be rejected by the upstream API unless the account has them
            // enabled. The gateway's beta filter handles stripping in production;
            // here we test the client detection + native pipeline path.
            let mut claude_code_headers = native_headers.clone();
            claude_code_headers.push(("anthropic-beta".into(), "claude-code-20250219".into()));
            claude_code_headers.push(("user-agent".into(), "claude-code/2.1.69 benchmark".into()));

            targets.push(Target {
                name: "proxy-claude-code".into(),
                url: format!("{proxy_base}/v1/messages"),
                headers: claude_code_headers,
                body: anthropic_request_body(proxy_anthropic_model),
            });
        }
    }

    // Run benchmarks
    for target in &targets {
        for (level, &concurrency) in config.concurrency_levels.iter().enumerate() {
            println!(
                "Running: {} @ concurrency={} ({} requests)...",
                target.name, concurrency, config.requests_per_level
            );

            let results = run_concurrent_benchmark(
                &client,
                &target.url,
                target.headers.clone(),
                &target.body,
                &target.name,
                concurrency,
                config.requests_per_level,
            )
            .await;

            // Print quick stats
            let ok_count = results.iter().filter(|r| r.error.is_none()).count();
            let err_count = results.iter().filter(|r| r.error.is_some()).count();
            if ok_count > 0 {
                let avg_e2e: f64 = results
                    .iter()
                    .filter(|r| r.error.is_none())
                    .map(|r| r.e2e_ms)
                    .sum::<f64>()
                    / ok_count as f64;
                let avg_ttfb: f64 = results
                    .iter()
                    .filter(|r| r.error.is_none())
                    .map(|r| r.ttfb_ms)
                    .sum::<f64>()
                    / ok_count as f64;
                println!(
                    "  OK: {ok_count}, Errors: {err_count}, Avg E2E: {avg_e2e:.1}ms, Avg TTFB: {avg_ttfb:.1}ms"
                );
            } else {
                println!("  All {err_count} requests failed");
                if let Some(first) = results.iter().find(|r| r.error.is_some()) {
                    println!(
                        "  First error (HTTP {}): {}",
                        first.status_code,
                        first.error.as_deref().unwrap_or_default()
                    );
                }
            }

            // A refusal the next level cannot change (bad key, no access,
            // unknown model): stop this target instead of repeating it.
            let refused = refused_outright(&results);
            all_results.extend(results);
            if refused && level + 1 < config.concurrency_levels.len() {
                println!(
                    "  Every request was refused (401/403/404): skipping the remaining levels for {}",
                    target.name
                );
                break;
            }
        }
    }

    // Compute summaries
    let mut summary_rows: Vec<SummaryRow> = Vec::new();
    for target in &targets {
        for &concurrency in &config.concurrency_levels {
            let row = compute_summary(&all_results, &target.name, concurrency);
            // A level skipped after an outright refusal ran nothing.
            if row.requests > 0 {
                summary_rows.push(row);
            }
        }
    }

    // Output results
    std::fs::create_dir_all(&config.output_dir).ok();
    let timestamp = chrono::Utc::now().format("%Y%m%d-%H%M%S");

    // JSON results
    let json_path = format!("{}/results-{timestamp}.json", config.output_dir);
    if let Ok(json_str) = serde_json::to_string_pretty(&all_results) {
        if let Ok(mut f) = std::fs::File::create(&json_path) {
            let _ = f.write_all(json_str.as_bytes());
            println!("\nRaw results: {json_path}");
        }
    }

    // Markdown summary
    let md_content = format_summary_table(&summary_rows);
    let md_path = format!("{}/summary-{timestamp}.md", config.output_dir);
    if let Ok(mut f) = std::fs::File::create(&md_path) {
        let _ = f.write_all(md_content.as_bytes());
        println!("Summary:     {md_path}");
    }

    // Print summary to stdout
    println!();
    println!("{md_content}");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ms(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    /// Was: the whole body was read before the first event was timed, so
    /// TTFB equalled end-to-end time. The first event is timed when it lands.
    #[test]
    fn ttfb_is_the_first_event_not_the_end_of_the_stream() {
        let mut r = SseReader::default();
        assert!(!r.feed(b"data: {\"choices\":[]}\n\n", ms(120)));
        assert!(!r.feed(b"data: {\"choices\":[]}\n\n", ms(900)));
        assert!(r.feed(b"data: [DONE]\n\n", ms(1300)));
        assert_eq!(r.ttfb, Some(ms(120)));
        assert_eq!(r.chunk_count, 2);
    }

    #[test]
    fn a_line_split_across_chunks_is_read_once_whole() {
        let mut r = SseReader::default();
        let line = "data: {\"usage\":{\"completion_tokens\":42},\"text\":\"é\"}\n";
        let bytes = line.as_bytes();
        // Split inside the multi-byte "é" as well as inside the JSON.
        let cut = line.find('é').unwrap() + 1;
        r.feed(&bytes[..10], ms(50));
        assert_eq!(r.ttfb, None, "no event until the line is complete");
        r.feed(&bytes[10..cut], ms(60));
        r.feed(&bytes[cut..], ms(70));
        assert_eq!(r.ttfb, Some(ms(70)));
        assert_eq!(r.completion_tokens, 42);
        assert_eq!(r.chunk_count, 1);
    }

    #[test]
    fn anthropic_usage_and_event_lines_are_read() {
        let mut r = SseReader::default();
        r.feed(
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            ms(200),
        );
        r.feed(
            b"event: message_delta\ndata: {\"usage\":{\"output_tokens\":37}}",
            ms(800),
        );
        r.finish(ms(810));
        assert_eq!(r.ttfb, Some(ms(200)));
        assert_eq!(r.completion_tokens, 37);
    }

    fn result(status: u16, error: bool) -> BenchmarkResult {
        BenchmarkResult {
            target: "t".into(),
            concurrency: 1,
            ttfb_ms: 0.0,
            e2e_ms: 0.0,
            completion_tokens: 0,
            tokens_per_sec: 0.0,
            status_code: status,
            error: error.then(|| "x".into()),
        }
    }

    #[test]
    fn only_a_refusal_no_level_can_change_stops_a_target() {
        assert!(refused_outright(&[result(401, true), result(403, true)]));
        assert!(!refused_outright(&[result(401, true), result(200, false)]));
        assert!(
            !refused_outright(&[result(502, true)]),
            "an upstream error may pass"
        );
        assert!(!refused_outright(&[]));
    }

    fn row(target: &str, requests: usize, errors: usize, p50: f64) -> SummaryRow {
        SummaryRow {
            target: target.into(),
            concurrency: 1,
            requests,
            p50_ms: p50,
            p95_ms: p50,
            p99_ms: p50,
            avg_ttfb_ms: p50,
            avg_tokens_per_sec: 0.0,
            errors,
        }
    }

    /// Was: a level where every proxied call failed was compared as 0 ms,
    /// printing a large negative "overhead".
    #[test]
    fn no_overhead_is_computed_against_a_level_with_no_successes() {
        let out = format_summary_table(&[
            row("openai-direct", 20, 0, 1200.0),
            row("proxy-openai", 20, 20, 0.0),
        ]);
        assert!(
            out.contains("| OpenAI (translation) | 1 | n/a | n/a | n/a |"),
            "{out}"
        );
        assert!(!out.contains("-1200"), "{out}");

        let out = format_summary_table(&[
            row("openai-direct", 20, 0, 1200.0),
            row("proxy-openai", 20, 0, 1203.5),
        ]);
        assert!(out.contains("+3.5ms"), "{out}");
    }
}
