// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Search Providers
//!
//! Pluggable backend trait and implementations for web search.
//! Each provider translates our unified search interface into the
//! provider-specific HTTP API.

use serde::{Deserialize, Serialize};
use serde_json::Value;

// =============================================================================
// Result types
// =============================================================================

#[derive(Debug, Clone, Serialize)]
pub struct SearchResult {
    pub title: String,
    pub url: String,
    pub snippet: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub published_date: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExtractResult {
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    pub text: String,
    pub word_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImageResult {
    pub title: String,
    pub url: String,
    pub image_url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thumbnail_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub width: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub height: Option<u32>,
}

// =============================================================================
// Search parameters
// =============================================================================

#[derive(Debug, Clone, Default, Deserialize)]
pub struct WebSearchParams {
    #[serde(default = "default_num_results")]
    pub num_results: u32,
    pub language: Option<String>,
    pub country: Option<String>,
    #[serde(default = "default_true")]
    pub safe_search: bool,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct NewsSearchParams {
    #[serde(default = "default_num_results")]
    pub num_results: u32,
    pub freshness: Option<String>, // "day", "week", "month"
    pub language: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct ExtractParams {
    pub max_length: Option<usize>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct ImageSearchParams {
    #[serde(default = "default_num_results")]
    pub num_results: u32,
    pub size: Option<String>, // "small", "medium", "large"
    #[serde(default = "default_true")]
    pub safe_search: bool,
}

fn default_num_results() -> u32 {
    5
}
fn default_true() -> bool {
    true
}

// =============================================================================
// Provider configuration (from DB row)
// =============================================================================

#[derive(Debug, Clone)]
pub struct SearchProviderConfig {
    pub provider: String,        // "brave", "google", "searxng"
    pub api_key: Option<String>, // decrypted
    pub google_cx_id: Option<String>,
    pub searxng_base_url: Option<String>,
    pub safe_search: bool,
    pub allowed_domains: Vec<String>,
    pub blocked_domains: Vec<String>,
    /// Replaces a hosted provider's origin (Brave, Google). Never set from
    /// configuration; the seam the tests point at a local server.
    pub api_base: Option<String>,
}

impl Default for SearchProviderConfig {
    fn default() -> Self {
        Self {
            provider: "brave".to_string(),
            api_key: None,
            google_cx_id: None,
            searxng_base_url: None,
            safe_search: true,
            allowed_domains: vec![],
            blocked_domains: vec![],
            api_base: None,
        }
    }
}

// =============================================================================
// Provider trait
// =============================================================================

#[derive(Debug)]
pub struct SearchError(pub String);

impl std::fmt::Display for SearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

pub type SearchRes<T> = Result<T, SearchError>;

/// Refuse a destination the outbound policy denies before fetching
/// it. `extract_content` fetches a URL taken from a tool call's arguments,
/// so the model (and whoever wrote its prompt) chooses it; the SearXNG base
/// URL is tenant-configured. The policy resolver covers hostnames, but an
/// IP literal (`http://169.254.169.254/…`) never reaches it.
fn endpoint(config: &SearchProviderConfig, origin: &str, path: &str) -> SearchRes<reqwest::Url> {
    let origin = config.api_base.as_deref().unwrap_or(origin);
    reqwest::Url::parse(&format!("{}{path}", origin.trim_end_matches('/')))
        .map_err(|e| SearchError(e.to_string()))
}

const BRAVE_ORIGIN: &str = "https://api.search.brave.com";
const GOOGLE_ORIGIN: &str = "https://www.googleapis.com";

fn check_destination(url: &str) -> SearchRes<()> {
    crate::services::outbound_policy::check_url(url).map_err(|denied| {
        tracing::warn!(host = %denied.host, reason = denied.reason, "search fetch refused by outbound policy");
        SearchError(denied.to_string())
    })
}

// =============================================================================
// Brave Search
// =============================================================================

pub async fn brave_web_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &WebSearchParams,
) -> SearchRes<Vec<SearchResult>> {
    let api_key = config
        .api_key
        .as_deref()
        .ok_or_else(|| SearchError("Brave API key not configured".into()))?;

    let mut url = endpoint(config, BRAVE_ORIGIN, "/res/v1/web/search")?;

    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("count", &params.num_results.min(20).to_string());

    if params.safe_search {
        url.query_pairs_mut().append_pair("safesearch", "strict");
    }
    if let Some(ref lang) = params.language {
        url.query_pairs_mut().append_pair("search_lang", lang);
    }
    if let Some(ref country) = params.country {
        url.query_pairs_mut().append_pair("country", country);
    }

    let resp = client
        .get(url)
        .header("X-Subscription-Token", api_key)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| SearchError(format!("Brave API request failed: {e}")))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(SearchError(format!("Brave API error {status}: {body}")));
    }

    let body: Value = resp
        .json()
        .await
        .map_err(|e| SearchError(format!("Failed to parse Brave response: {e}")))?;

    let results = body["web"]["results"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| SearchResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["url"].as_str().unwrap_or("").to_string(),
                    snippet: r["description"].as_str().unwrap_or("").to_string(),
                    published_date: r["page_age"].as_str().map(String::from),
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(filter_domains(results, config))
}

pub async fn brave_news_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &NewsSearchParams,
) -> SearchRes<Vec<SearchResult>> {
    let api_key = config
        .api_key
        .as_deref()
        .ok_or_else(|| SearchError("Brave API key not configured".into()))?;

    let mut url = endpoint(config, BRAVE_ORIGIN, "/res/v1/news/search")?;

    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("count", &params.num_results.min(20).to_string());

    if let Some(ref freshness) = params.freshness {
        url.query_pairs_mut().append_pair("freshness", freshness);
    }

    let resp = client
        .get(url)
        .header("X-Subscription-Token", api_key)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| SearchError(format!("Brave news API failed: {e}")))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(SearchError(format!(
            "Brave news API error {status}: {body}"
        )));
    }

    let body: Value = resp
        .json()
        .await
        .map_err(|e| SearchError(format!("Failed to parse Brave news response: {e}")))?;

    let results = body["results"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| SearchResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["url"].as_str().unwrap_or("").to_string(),
                    snippet: r["description"].as_str().unwrap_or("").to_string(),
                    published_date: r["age"].as_str().map(String::from),
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(filter_domains(results, config))
}

pub async fn brave_image_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &ImageSearchParams,
) -> SearchRes<Vec<ImageResult>> {
    let api_key = config
        .api_key
        .as_deref()
        .ok_or_else(|| SearchError("Brave API key not configured".into()))?;

    let mut url = endpoint(config, BRAVE_ORIGIN, "/res/v1/images/search")?;

    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("count", &params.num_results.min(20).to_string());

    if params.safe_search {
        url.query_pairs_mut().append_pair("safesearch", "strict");
    }

    let resp = client
        .get(url)
        .header("X-Subscription-Token", api_key)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| SearchError(format!("Brave image API failed: {e}")))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(SearchError(format!(
            "Brave image API error {status}: {body}"
        )));
    }

    let body: Value = resp
        .json()
        .await
        .map_err(|e| SearchError(format!("Failed to parse Brave image response: {e}")))?;

    let results = body["results"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| ImageResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["url"].as_str().unwrap_or("").to_string(),
                    image_url: r["properties"]["url"].as_str().unwrap_or("").to_string(),
                    thumbnail_url: r["thumbnail"]["src"].as_str().map(String::from),
                    width: r["properties"]["width"].as_u64().map(|v| v as u32),
                    height: r["properties"]["height"].as_u64().map(|v| v as u32),
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(filter_image_domains(results, config))
}

// =============================================================================
// Google Custom Search
// =============================================================================

pub async fn google_web_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &WebSearchParams,
) -> SearchRes<Vec<SearchResult>> {
    let api_key = config
        .api_key
        .as_deref()
        .ok_or_else(|| SearchError("Google API key not configured".into()))?;
    let cx = config
        .google_cx_id
        .as_deref()
        .ok_or_else(|| SearchError("Google Custom Search Engine ID (CX) not configured".into()))?;

    let mut url = endpoint(config, GOOGLE_ORIGIN, "/customsearch/v1")?;

    url.query_pairs_mut()
        .append_pair("key", api_key)
        .append_pair("cx", cx)
        .append_pair("q", query)
        .append_pair("num", &params.num_results.min(10).to_string());

    if params.safe_search {
        url.query_pairs_mut().append_pair("safe", "active");
    }

    let resp = client
        .get(url)
        .send()
        .await
        // `without_url`: the API key is in the query string, and this message goes
        // back to the caller as the tool's error.
        .map_err(|e| SearchError(format!("Google API request failed: {}", e.without_url())))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(SearchError(format!("Google API error {status}: {body}")));
    }

    let body: Value = resp.json().await.map_err(|e| {
        SearchError(format!(
            "Failed to parse Google response: {}",
            e.without_url()
        ))
    })?;

    let results = body["items"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| SearchResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["link"].as_str().unwrap_or("").to_string(),
                    snippet: r["snippet"].as_str().unwrap_or("").to_string(),
                    published_date: None,
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(filter_domains(results, config))
}

pub async fn google_image_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &ImageSearchParams,
) -> SearchRes<Vec<ImageResult>> {
    let api_key = config
        .api_key
        .as_deref()
        .ok_or_else(|| SearchError("Google API key not configured".into()))?;
    let cx = config
        .google_cx_id
        .as_deref()
        .ok_or_else(|| SearchError("Google CX not configured".into()))?;

    let mut url = endpoint(config, GOOGLE_ORIGIN, "/customsearch/v1")?;

    url.query_pairs_mut()
        .append_pair("key", api_key)
        .append_pair("cx", cx)
        .append_pair("q", query)
        .append_pair("searchType", "image")
        .append_pair("num", &params.num_results.min(10).to_string());

    if params.safe_search {
        url.query_pairs_mut().append_pair("safe", "active");
    }

    let resp = client
        .get(url)
        .send()
        .await
        .map_err(|e| SearchError(format!("Google image API failed: {}", e.without_url())))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(SearchError(format!(
            "Google image API error {status}: {body}"
        )));
    }

    let body: Value = resp.json().await.map_err(|e| {
        SearchError(format!(
            "Failed to parse Google image response: {}",
            e.without_url()
        ))
    })?;

    let results = body["items"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| ImageResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["image"]["contextLink"].as_str().unwrap_or("").to_string(),
                    image_url: r["link"].as_str().unwrap_or("").to_string(),
                    thumbnail_url: r["image"]["thumbnailLink"].as_str().map(String::from),
                    width: r["image"]["width"].as_u64().map(|v| v as u32),
                    height: r["image"]["height"].as_u64().map(|v| v as u32),
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(filter_image_domains(results, config))
}

// =============================================================================
// SearXNG (self-hosted)
// =============================================================================

pub async fn searxng_web_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &WebSearchParams,
) -> SearchRes<Vec<SearchResult>> {
    let base = config
        .searxng_base_url
        .as_deref()
        .ok_or_else(|| SearchError("SearXNG base URL not configured".into()))?;

    let mut url = reqwest::Url::parse(&format!("{}/search", base.trim_end_matches('/')))
        .map_err(|e| SearchError(e.to_string()))?;
    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("format", "json")
        .append_pair("categories", "general")
        .append_pair("pageno", "1");
    check_destination(url.as_str())?;

    let resp = client
        .get(url.as_str())
        .send()
        .await
        .map_err(|e| SearchError(format!("SearXNG request failed: {e}")))?;

    let body: Value = resp
        .json()
        .await
        .map_err(|e| SearchError(format!("Failed to parse SearXNG response: {e}")))?;

    let results: Vec<SearchResult> = body["results"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| SearchResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["url"].as_str().unwrap_or("").to_string(),
                    snippet: r["content"].as_str().unwrap_or("").to_string(),
                    published_date: r["publishedDate"].as_str().map(String::from),
                })
                .collect()
        })
        .unwrap_or_default();

    let mut filtered = filter_domains(results, config);
    filtered.truncate(params.num_results as usize);
    Ok(filtered)
}

pub async fn searxng_news_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &NewsSearchParams,
) -> SearchRes<Vec<SearchResult>> {
    let base = config
        .searxng_base_url
        .as_deref()
        .ok_or_else(|| SearchError("SearXNG base URL not configured".into()))?;

    let mut url = reqwest::Url::parse(&format!("{}/search", base.trim_end_matches('/')))
        .map_err(|e| SearchError(e.to_string()))?;
    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("format", "json")
        .append_pair("categories", "news")
        .append_pair("pageno", "1");
    check_destination(url.as_str())?;

    let resp = client
        .get(url.as_str())
        .send()
        .await
        .map_err(|e| SearchError(format!("SearXNG news request failed: {e}")))?;

    let body: Value = resp
        .json()
        .await
        .map_err(|e| SearchError(format!("Failed to parse SearXNG news response: {e}")))?;

    let results = body["results"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| SearchResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["url"].as_str().unwrap_or("").to_string(),
                    snippet: r["content"].as_str().unwrap_or("").to_string(),
                    published_date: r["publishedDate"].as_str().map(String::from),
                })
                .collect()
        })
        .unwrap_or_default();

    let mut filtered = filter_domains(results, config);
    filtered.truncate(params.num_results as usize);
    Ok(filtered)
}

pub async fn searxng_image_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &ImageSearchParams,
) -> SearchRes<Vec<ImageResult>> {
    let base = config
        .searxng_base_url
        .as_deref()
        .ok_or_else(|| SearchError("SearXNG base URL not configured".into()))?;

    let mut url = reqwest::Url::parse(&format!("{}/search", base.trim_end_matches('/')))
        .map_err(|e| SearchError(e.to_string()))?;
    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("format", "json")
        .append_pair("categories", "images")
        .append_pair("pageno", "1");
    check_destination(url.as_str())?;

    let resp = client
        .get(url.as_str())
        .send()
        .await
        .map_err(|e| SearchError(format!("SearXNG image request failed: {e}")))?;

    let body: Value = resp
        .json()
        .await
        .map_err(|e| SearchError(format!("Failed to parse SearXNG image response: {e}")))?;

    let results = body["results"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|r| ImageResult {
                    title: r["title"].as_str().unwrap_or("").to_string(),
                    url: r["url"].as_str().unwrap_or("").to_string(),
                    image_url: r["img_src"].as_str().unwrap_or("").to_string(),
                    thumbnail_url: r["thumbnail_src"].as_str().map(String::from),
                    width: None,
                    height: None,
                })
                .collect()
        })
        .unwrap_or_default();

    let mut filtered = filter_image_domains(results, config);
    filtered.truncate(params.num_results as usize);
    Ok(filtered)
}

// =============================================================================
// Content extraction (provider-agnostic — uses direct HTTP fetch + HTML cleanup)
// =============================================================================

/// Fetches with [`outbound_policy::public_fetch_client`]: the URL is any web
/// page, which may redirect across origins, and no credential is sent.
///
/// [`outbound_policy::public_fetch_client`]: crate::services::outbound_policy::public_fetch_client
pub async fn extract_content(url: &str, params: &ExtractParams) -> SearchRes<ExtractResult> {
    check_destination(url)?;
    let resp = crate::services::outbound_policy::public_fetch_client()
        .get(url)
        .header("User-Agent", "Mozilla/5.0 (compatible; BrutorBot/1.0)")
        .send()
        .await
        .map_err(|e| SearchError(format!("Failed to fetch URL: {e}")))?;

    if !resp.status().is_success() {
        return Err(SearchError(format!(
            "HTTP {} fetching {url}",
            resp.status()
        )));
    }

    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();

    let body = resp
        .text()
        .await
        .map_err(|e| SearchError(format!("Failed to read response body: {e}")))?;

    // Try to extract title from HTML (before consuming body)
    let title = if content_type.contains("html") {
        extract_html_title(&body)
    } else {
        None
    };

    // Simple HTML to text extraction (strip tags)
    let text = if content_type.contains("html") {
        strip_html_tags(&body)
    } else {
        body
    };

    // Truncate if max_length specified
    let text = match params.max_length {
        // On a character boundary: `max` comes from the tool call, and a byte
        // inside a multi-byte character would panic the request.
        Some(max) if text.len() > max => text[..text.floor_char_boundary(max)].to_string(),
        _ => text,
    };

    let word_count = text.split_whitespace().count();

    Ok(ExtractResult {
        url: url.to_string(),
        title,
        text,
        word_count,
    })
}

// =============================================================================
// Helpers
// =============================================================================

/// Filter results by allowed/blocked domains.
fn filter_domains(results: Vec<SearchResult>, config: &SearchProviderConfig) -> Vec<SearchResult> {
    results
        .into_iter()
        .filter(|r| domain_permitted(&r.url, config))
        .collect()
}

/// [`filter_domains`] for images: an image is shown from `image_url`, so the
/// page it sits on and the host serving it must both pass.
fn filter_image_domains(
    results: Vec<ImageResult>,
    config: &SearchProviderConfig,
) -> Vec<ImageResult> {
    results
        .into_iter()
        .filter(|r| domain_permitted(&r.url, config) && domain_permitted(&r.image_url, config))
        .collect()
}

pub fn domain_permitted(url: &str, config: &SearchProviderConfig) -> bool {
    let domain = extract_domain(url);
    // If allowed_domains is non-empty, only allow those
    if !config.allowed_domains.is_empty() {
        return config
            .allowed_domains
            .iter()
            .any(|d| domain_matches(&domain, d));
    }
    !config
        .blocked_domains
        .iter()
        .any(|d| domain_matches(&domain, d))
}

/// The domain itself or a subdomain. A bare suffix match would let
/// `notexample.com` through an allow-list for `example.com`.
fn domain_matches(host: &str, domain: &str) -> bool {
    let (host, domain) = (host.to_ascii_lowercase(), domain.to_ascii_lowercase());
    let domain = domain.trim_start_matches('.');
    !domain.is_empty()
        && (host == domain
            || host
                .strip_suffix(domain)
                .is_some_and(|rest| rest.ends_with('.')))
}

fn extract_domain(url: &str) -> String {
    reqwest::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(String::from))
        .unwrap_or_default()
}

/// Simple HTML tag stripper (no external dependency).
fn strip_html_tags(html: &str) -> String {
    let mut result = String::with_capacity(html.len());
    let mut in_tag = false;
    let mut in_script = false;
    let mut in_style = false;

    // ASCII only: full lowercasing changes the length of some text ('İ'), and
    // the indexes below are shared between the two.
    let lower = html.to_ascii_lowercase();
    let chars: Vec<char> = html.chars().collect();
    let lower_chars: Vec<char> = lower.chars().collect();

    let mut i = 0;
    while i < chars.len() {
        if !in_tag && i + 7 < lower_chars.len() {
            let ahead: String = lower_chars[i..i + 7].iter().collect();
            if ahead == "<script" {
                in_script = true;
            }
            if ahead == "<style "
                || (i + 6 < lower_chars.len()
                    && lower_chars[i..i + 6].iter().collect::<String>() == "<style")
            {
                in_style = true;
            }
        }

        if chars[i] == '<' {
            in_tag = true;
            if i + 9 < lower_chars.len() {
                let ahead: String = lower_chars[i..i + 9].iter().collect();
                if ahead == "</script>" {
                    in_script = false;
                }
            }
            if i + 8 < lower_chars.len() {
                let ahead: String = lower_chars[i..i + 8].iter().collect();
                if ahead == "</style>" {
                    in_style = false;
                }
            }
        } else if chars[i] == '>' {
            in_tag = false;
        } else if !in_tag && !in_script && !in_style {
            result.push(chars[i]);
        }

        i += 1;
    }

    // Collapse whitespace
    let mut collapsed = String::with_capacity(result.len());
    let mut last_was_ws = false;
    for ch in result.chars() {
        if ch.is_whitespace() {
            if !last_was_ws {
                collapsed.push(' ');
                last_was_ws = true;
            }
        } else {
            collapsed.push(ch);
            last_was_ws = false;
        }
    }

    collapsed.trim().to_string()
}

fn extract_html_title(html: &str) -> Option<String> {
    // ASCII only: the offsets found here slice `html`.
    let lower = html.to_ascii_lowercase();
    let start = lower.find("<title>")?;
    let end = lower[start..].find("</title>")?;
    let title = &html[start + 7..start + end];
    let title = title.trim();
    if title.is_empty() {
        None
    } else {
        Some(title.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{header, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn hosted(provider: &str, mock: &MockServer) -> SearchProviderConfig {
        SearchProviderConfig {
            provider: provider.into(),
            api_key: Some("api-key-1".into()),
            google_cx_id: Some("cx-1".into()),
            api_base: Some(mock.uri()),
            ..Default::default()
        }
    }

    fn searxng(mock: &MockServer) -> SearchProviderConfig {
        SearchProviderConfig {
            provider: "searxng".into(),
            searxng_base_url: Some(format!("{}/", mock.uri())),
            ..Default::default()
        }
    }

    fn web(n: u32) -> WebSearchParams {
        WebSearchParams {
            num_results: n,
            language: Some("de".into()),
            country: Some("DE".into()),
            safe_search: true,
        }
    }

    fn news(n: u32) -> NewsSearchParams {
        NewsSearchParams {
            num_results: n,
            freshness: Some("day".into()),
            language: None,
        }
    }

    fn images(n: u32) -> ImageSearchParams {
        ImageSearchParams {
            num_results: n,
            size: None,
            safe_search: true,
        }
    }

    async fn answering(p: &str, body: Value) -> MockServer {
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path(p))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(&mock)
            .await;
        mock
    }

    async fn failing(status: u16, body: &str) -> MockServer {
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(status).set_body_string(body))
            .mount(&mock)
            .await;
        mock
    }

    #[tokio::test]
    async fn brave_web_sends_the_key_as_a_header_and_caps_the_count() {
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/res/v1/web/search"))
            .and(header("X-Subscription-Token", "api-key-1"))
            .and(query_param("q", "rust"))
            .and(query_param("count", "20"))
            .and(query_param("safesearch", "strict"))
            .and(query_param("search_lang", "de"))
            .and(query_param("country", "DE"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "web": {"results": [
                    {"title": "T", "url": "https://a.example/x", "description": "S", "page_age": "2024"},
                ]},
            })))
            .expect(1)
            .mount(&mock)
            .await;
        let got = brave_web_search(
            &reqwest::Client::new(),
            &hosted("brave", &mock),
            "rust",
            &web(99),
        )
        .await
        .unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].title, "T");
        assert_eq!(got[0].snippet, "S");
        assert_eq!(got[0].published_date.as_deref(), Some("2024"));
    }

    #[tokio::test]
    async fn brave_news_and_images_are_parsed() {
        let c = reqwest::Client::new();
        let mock = answering(
            "/res/v1/news/search",
            json!({"results": [{"title": "N", "url": "https://a.example/n", "description": "D", "age": "1h"}]}),
        )
        .await;
        let got = brave_news_search(&c, &hosted("brave", &mock), "q", &news(3))
            .await
            .unwrap();
        assert_eq!(got[0].published_date.as_deref(), Some("1h"));

        let mock = answering(
            "/res/v1/images/search",
            json!({"results": [{
                "title": "I", "url": "https://a.example/page",
                "properties": {"url": "https://a.example/i.png", "width": 10, "height": 20},
                "thumbnail": {"src": "https://t.example/t.png"},
            }]}),
        )
        .await;
        let got = brave_image_search(&c, &hosted("brave", &mock), "q", &images(3))
            .await
            .unwrap();
        assert_eq!(got[0].image_url, "https://a.example/i.png");
        assert_eq!(got[0].width, Some(10));
        assert_eq!(
            got[0].thumbnail_url.as_deref(),
            Some("https://t.example/t.png")
        );
    }

    #[tokio::test]
    async fn google_web_and_images_are_parsed() {
        let c = reqwest::Client::new();
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/customsearch/v1"))
            .and(query_param("cx", "cx-1"))
            .and(query_param("num", "10"))
            .and(query_param("safe", "active"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "items": [{"title": "G", "link": "https://g.example/1", "snippet": "S"}],
            })))
            .mount(&mock)
            .await;
        let got = google_web_search(&c, &hosted("google", &mock), "q", &web(50))
            .await
            .unwrap();
        assert_eq!(got[0].url, "https://g.example/1");

        let mock = answering(
            "/customsearch/v1",
            json!({"items": [{
                "title": "G", "link": "https://g.example/i.png",
                "image": {"contextLink": "https://g.example/p", "thumbnailLink": "https://g.example/t", "width": 7, "height": 8},
            }]}),
        )
        .await;
        let got = google_image_search(&c, &hosted("google", &mock), "q", &images(50))
            .await
            .unwrap();
        assert_eq!(got[0].url, "https://g.example/p");
        assert_eq!(got[0].height, Some(8));
    }

    #[tokio::test]
    async fn searxng_parses_every_category_and_respects_the_count() {
        let c = reqwest::Client::new();
        let many: Vec<Value> = (0..4)
            .map(|i| {
                json!({"title": format!("r{i}"), "url": format!("https://s.example/{i}"),
                       "content": "c", "publishedDate": "d",
                       "img_src": format!("https://s.example/{i}.png"), "thumbnail_src": "t"})
            })
            .collect();
        let mock = answering("/search", json!({"results": many})).await;
        let cfg = searxng(&mock);
        assert_eq!(
            searxng_web_search(&c, &cfg, "q", &web(2))
                .await
                .unwrap()
                .len(),
            2
        );
        assert_eq!(
            searxng_news_search(&c, &cfg, "q", &news(3))
                .await
                .unwrap()
                .len(),
            3
        );
        let imgs = searxng_image_search(&c, &cfg, "q", &images(1))
            .await
            .unwrap();
        assert_eq!(imgs.len(), 1);
        assert_eq!(imgs[0].image_url, "https://s.example/0.png");
    }

    /// Filtering happens before the count, or a blocked result eats a slot.
    #[tokio::test]
    async fn the_count_is_taken_after_filtering() {
        let c = reqwest::Client::new();
        let mock = answering(
            "/search",
            json!({"results": [
                {"url": "https://blocked.example/1"},
                {"url": "https://ok.example/2"},
            ]}),
        )
        .await;
        let mut cfg = searxng(&mock);
        cfg.blocked_domains = vec!["blocked.example".into()];
        let got = searxng_web_search(&c, &cfg, "q", &web(1)).await.unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].url, "https://ok.example/2");
    }

    /// The tenant's domain policy applies to every kind of search, not just
    /// the web tab.
    #[tokio::test]
    async fn domain_policy_applies_to_news_and_images_on_every_provider() {
        let c = reqwest::Client::new();
        let body = json!({"results": [
            {"title": "bad", "url": "https://blocked.example/1", "img_src": "https://ok.example/i.png",
             "properties": {"url": "https://ok.example/i.png"}, "description": "x", "content": "x"},
            {"title": "bad-host", "url": "https://ok.example/2", "img_src": "https://cdn.blocked.example/i.png",
             "properties": {"url": "https://cdn.blocked.example/i.png"}, "description": "x", "content": "x"},
            {"title": "good", "url": "https://ok.example/3", "img_src": "https://ok.example/i.png",
             "properties": {"url": "https://ok.example/i.png"}, "description": "x", "content": "x"},
        ]});
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(body.clone()))
            .mount(&mock)
            .await;
        let blocked = vec!["blocked.example".to_string()];
        let mut sx = searxng(&mock);
        sx.blocked_domains = blocked.clone();
        let mut brave = hosted("brave", &mock);
        brave.blocked_domains = blocked.clone();

        let n = searxng_news_search(&c, &sx, "q", &news(9)).await.unwrap();
        assert_eq!(n.len(), 2, "{n:?}");
        let i = searxng_image_search(&c, &sx, "q", &images(9))
            .await
            .unwrap();
        assert_eq!(i.len(), 1, "{i:?}");
        assert_eq!(i[0].title, "good");
        let i = brave_image_search(&c, &brave, "q", &images(9))
            .await
            .unwrap();
        assert_eq!(i.len(), 1, "{i:?}");

        let g = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"items": [
                {"title": "bad", "link": "https://blocked.example/i.png", "image": {"contextLink": "https://ok.example/p"}},
                {"title": "good", "link": "https://ok.example/i.png", "image": {"contextLink": "https://ok.example/p"}},
            ]})))
            .mount(&g)
            .await;
        let mut google = hosted("google", &g);
        google.blocked_domains = blocked;
        let i = google_image_search(&c, &google, "q", &images(9))
            .await
            .unwrap();
        assert_eq!(i.len(), 1, "{i:?}");
        assert_eq!(i[0].title, "good");
    }

    #[test]
    fn a_domain_matches_itself_and_its_subdomains_only() {
        assert!(domain_matches("example.com", "example.com"));
        assert!(domain_matches("a.b.example.com", "example.com"));
        assert!(domain_matches("EXAMPLE.com", "example.COM"));
        assert!(domain_matches("a.example.com", ".example.com"));
        assert!(!domain_matches("notexample.com", "example.com"));
        assert!(!domain_matches("example.com.evil.net", "example.com"));
        assert!(!domain_matches("example.com", ""));

        let allow = SearchProviderConfig {
            allowed_domains: vec!["example.com".into()],
            blocked_domains: vec!["example.com".into()],
            ..Default::default()
        };
        assert!(
            domain_permitted("https://a.example.com/x", &allow),
            "allow-list wins"
        );
        assert!(!domain_permitted("https://evilexample.com/x", &allow));
        assert!(!domain_permitted("not a url", &allow), "no host, no allow");
        let block = SearchProviderConfig {
            blocked_domains: vec!["example.com".into()],
            ..Default::default()
        };
        assert!(!domain_permitted("https://a.example.com/x", &block));
        assert!(domain_permitted("https://notexample.com/x", &block));
    }

    #[tokio::test]
    async fn a_missing_credential_or_base_is_an_error_not_a_call() {
        let c = reqwest::Client::new();
        let none = SearchProviderConfig::default();
        assert!(brave_web_search(&c, &none, "q", &web(1)).await.is_err());
        assert!(brave_news_search(&c, &none, "q", &news(1)).await.is_err());
        assert!(
            brave_image_search(&c, &none, "q", &images(1))
                .await
                .is_err()
        );
        assert!(google_web_search(&c, &none, "q", &web(1)).await.is_err());
        assert!(
            google_image_search(&c, &none, "q", &images(1))
                .await
                .is_err()
        );
        let no_cx = SearchProviderConfig {
            api_key: Some("k".into()),
            ..Default::default()
        };
        assert!(google_web_search(&c, &no_cx, "q", &web(1)).await.is_err());
        assert!(
            google_image_search(&c, &no_cx, "q", &images(1))
                .await
                .is_err()
        );
        assert!(searxng_web_search(&c, &none, "q", &web(1)).await.is_err());
        assert!(searxng_news_search(&c, &none, "q", &news(1)).await.is_err());
        assert!(
            searxng_image_search(&c, &none, "q", &images(1))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn a_provider_error_surfaces_with_its_status() {
        let c = reqwest::Client::new();
        let mock = failing(429, "slow down").await;
        let cfg = hosted("brave", &mock);
        for e in [
            brave_web_search(&c, &cfg, "q", &web(1))
                .await
                .unwrap_err()
                .0,
            brave_news_search(&c, &cfg, "q", &news(1))
                .await
                .unwrap_err()
                .0,
            brave_image_search(&c, &cfg, "q", &images(1))
                .await
                .err()
                .unwrap()
                .0,
            google_web_search(&c, &cfg, "q", &web(1))
                .await
                .unwrap_err()
                .0,
            google_image_search(&c, &cfg, "q", &images(1))
                .await
                .err()
                .unwrap()
                .0,
        ] {
            assert!(e.contains("429"), "{e}");
            assert!(e.contains("slow down"), "{e}");
        }
    }

    #[tokio::test]
    async fn an_unparseable_answer_is_an_error_not_an_empty_list() {
        let c = reqwest::Client::new();
        let mock = failing(200, "<html>not json</html>").await;
        let h = hosted("brave", &mock);
        assert!(brave_web_search(&c, &h, "q", &web(1)).await.is_err());
        assert!(brave_news_search(&c, &h, "q", &news(1)).await.is_err());
        assert!(brave_image_search(&c, &h, "q", &images(1)).await.is_err());
        assert!(google_web_search(&c, &h, "q", &web(1)).await.is_err());
        assert!(google_image_search(&c, &h, "q", &images(1)).await.is_err());
        let s = searxng(&mock);
        assert!(searxng_web_search(&c, &s, "q", &web(1)).await.is_err());
        assert!(searxng_news_search(&c, &s, "q", &news(1)).await.is_err());
        assert!(searxng_image_search(&c, &s, "q", &images(1)).await.is_err());
    }

    /// Google takes its key in the URL, and the error goes back to the caller.
    #[tokio::test]
    async fn the_google_key_stays_out_of_a_transport_error() {
        let c = reqwest::Client::new();
        let cfg = SearchProviderConfig {
            api_key: Some("SECRET-GOOGLE-KEY".into()),
            google_cx_id: Some("cx".into()),
            api_base: Some("http://127.0.0.1:1".into()),
            ..Default::default()
        };
        let e = google_web_search(&c, &cfg, "q", &web(1))
            .await
            .unwrap_err()
            .0;
        assert!(!e.contains("SECRET-GOOGLE-KEY"), "{e}");
        let e = google_image_search(&c, &cfg, "q", &images(1))
            .await
            .err()
            .unwrap()
            .0;
        assert!(!e.contains("SECRET-GOOGLE-KEY"), "{e}");
        let fails = failing(200, "not json").await;
        let cfg = SearchProviderConfig {
            api_base: Some(fails.uri()),
            ..cfg
        };
        let e = google_web_search(&c, &cfg, "q", &web(1))
            .await
            .unwrap_err()
            .0;
        assert!(!e.contains("SECRET-GOOGLE-KEY"), "{e}");
        let e = google_image_search(&c, &cfg, "q", &images(1))
            .await
            .err()
            .unwrap()
            .0;
        assert!(!e.contains("SECRET-GOOGLE-KEY"), "{e}");
    }

    #[tokio::test]
    async fn the_transport_failing_is_an_error_on_every_provider() {
        let c = reqwest::Client::new();
        let cfg = SearchProviderConfig {
            api_key: Some("k".into()),
            api_base: Some("http://127.0.0.1:1".into()),
            searxng_base_url: Some("http://127.0.0.1:1".into()),
            ..Default::default()
        };
        assert!(brave_web_search(&c, &cfg, "q", &web(1)).await.is_err());
        assert!(brave_news_search(&c, &cfg, "q", &news(1)).await.is_err());
        assert!(brave_image_search(&c, &cfg, "q", &images(1)).await.is_err());
        assert!(searxng_web_search(&c, &cfg, "q", &web(1)).await.is_err());
        assert!(searxng_news_search(&c, &cfg, "q", &news(1)).await.is_err());
        assert!(
            searxng_image_search(&c, &cfg, "q", &images(1))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn extract_reads_html_and_plain_text() {
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/page"))
            .respond_with(ResponseTemplate::new(200).set_body_raw(
                "<html><head><TITLE> The  Title </TITLE><style>x{}</style></head>                 <body><script>var a = 1;</script><p>Hello   <b>world</b></p></body></html>",
                "text/html; charset=utf-8",
            ))
            .mount(&mock)
            .await;
        Mock::given(method("GET"))
            .and(path("/text"))
            .respond_with(
                ResponseTemplate::new(200).set_body_raw("plain <b>kept</b>", "text/plain"),
            )
            .mount(&mock)
            .await;
        Mock::given(method("GET"))
            .and(path("/gone"))
            .respond_with(ResponseTemplate::new(404))
            .mount(&mock)
            .await;

        let p = ExtractParams::default();
        let got = extract_content(&format!("{}/page", mock.uri()), &p)
            .await
            .unwrap();
        assert_eq!(got.title.as_deref(), Some("The  Title"));
        assert!(got.text.contains("Hello world"), "{}", got.text);
        assert!(!got.text.contains("var a"), "{}", got.text);
        assert_eq!(got.word_count, got.text.split_whitespace().count());

        let got = extract_content(&format!("{}/text", mock.uri()), &p)
            .await
            .unwrap();
        assert!(got.title.is_none());
        assert_eq!(got.text, "plain <b>kept</b>");

        let e = extract_content(&format!("{}/gone", mock.uri()), &p)
            .await
            .unwrap_err()
            .0;
        assert!(e.contains("404"), "{e}");
        let e = extract_content("http://127.0.0.1:1/x", &p)
            .await
            .unwrap_err()
            .0;
        assert!(e.contains("Failed to fetch"), "{e}");
    }

    /// `max_length` comes from the tool call: cutting inside a multi-byte
    /// character must not panic the request.
    #[tokio::test]
    async fn truncation_lands_on_a_character_boundary() {
        let mock = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_raw("ééééé", "text/plain"))
            .mount(&mock)
            .await;
        let p = ExtractParams {
            max_length: Some(3),
        };
        let got = extract_content(&mock.uri(), &p).await.unwrap();
        assert_eq!(got.text, "é");
        let p = ExtractParams {
            max_length: Some(1000),
        };
        assert_eq!(
            extract_content(&mock.uri(), &p).await.unwrap().text,
            "ééééé"
        );
    }

    /// Text whose lowercase form has a different byte length must not shift
    /// the slices (a panic, on any page, from a web fetch).
    #[test]
    fn html_with_length_changing_case_does_not_panic() {
        let html = "<html><title>İİİ title</title><body>İ<script>x</script>y</body></html>";
        assert_eq!(extract_html_title(html).as_deref(), Some("İİİ title"));
        let text = strip_html_tags(html);
        assert!(text.contains("İ"), "{text}");
        assert!(!text.contains('x'), "{text}");
        assert_eq!(extract_html_title("<title> </title>"), None);
        assert_eq!(extract_html_title("<p>none</p>"), None);
    }

    #[test]
    fn stripping_removes_tags_scripts_and_styles_and_collapses_space() {
        assert_eq!(
            strip_html_tags("<p>a\n\n  b</p><style media=x>c{}</style><SCRIPT>d</SCRIPT> e"),
            "a b e"
        );
        assert_eq!(strip_html_tags(""), "");
    }

    #[test]
    fn extract_domain_handles_garbage() {
        assert_eq!(extract_domain("https://a.example/x"), "a.example");
        assert_eq!(extract_domain("nope"), "");
    }

    #[test]
    fn params_default_from_json() {
        let w: WebSearchParams = serde_json::from_value(json!({})).unwrap();
        assert_eq!(w.num_results, 5);
        assert!(w.safe_search);
        let n: NewsSearchParams = serde_json::from_value(json!({})).unwrap();
        assert_eq!(n.num_results, 5);
        let i: ImageSearchParams = serde_json::from_value(json!({"safe_search": false})).unwrap();
        assert!(!i.safe_search);
        assert_eq!(SearchError("boom".into()).to_string(), "boom");
    }

    /// The extract tool's URL comes from the model's tool call; a
    /// metadata-service URL is refused before any request is made (the
    /// client below could not connect anywhere useful either way, so the
    /// error text is what proves the refusal came first).
    #[tokio::test]
    async fn extract_content_refuses_the_metadata_service() {
        let params = ExtractParams::default();
        let err = extract_content("http://169.254.169.254/latest/meta-data/", &params)
            .await
            .expect_err("denied");
        assert!(err.0.contains("refused by policy"), "{}", err.0);
    }

    #[tokio::test]
    async fn searxng_with_a_denied_base_url_is_refused() {
        let client = reqwest::Client::new();
        let config = SearchProviderConfig {
            provider: "searxng".into(),
            searxng_base_url: Some("http://[fd00:ec2::254]".into()),
            ..Default::default()
        };
        let params = WebSearchParams::default();
        let err = searxng_web_search(&client, &config, "q", &params)
            .await
            .expect_err("denied");
        assert!(err.0.contains("refused by policy"), "{}", err.0);
    }
}
