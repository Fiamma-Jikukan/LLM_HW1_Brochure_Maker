from bs4 import BeautifulSoup
import requests


# Standard headers to fetch a website
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
}


def fetch_website_contents(url):
    """
    Return the title and contents of the website at the given url;
    truncate to 2,000 characters as a sensible limit
    """
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, "html.parser")
    title = soup.title.string if soup.title else "No title found"
    if soup.body:
        for irrelevant in soup.body(["script", "style", "img", "input"]):
            irrelevant.decompose()
        text = soup.body.get_text(separator="\n", strip=True)
    else:
        text = ""
    return (title + "\n\n" + text)[:2_000]


def fetch_website_links(url):
    """
    Return the links on the webiste at the given url
    I realize this is inefficient as we're parsing twice! This is to keep the code in the lab simple.
    Feel free to use a class and optimize it!
    """
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, "html.parser")
    links = [link.get("href") for link in soup.find_all("a")]
    return [link for link in links if link]


# ---------------------------------------------------------------------------
# Added for assignment 1 (the two functions above are unchanged class code).
#
# fetch_page() differs from fetch_website_contents() / fetch_website_links():
#   - one request per page, returning both the text and the links
#   - the full text, not cut to 2,000 characters (the context budget is applied later)
#   - a timeout, and request errors are caught instead of crashing the run
#   - the HTTP status and content type are checked, so an error page is not used as content
#   - the final URL after redirects is kept, to resolve relative links correctly
# ---------------------------------------------------------------------------


def fetch_page(url, timeout=15):
    """
    Fetch one page and return a dict:
      requested_url, final_url, http_status, content_type, title, text, links, error
    `error` is None on success; otherwise it explains why the page is unusable.
    """
    result = {
        "requested_url": url,
        "final_url": None,
        "http_status": None,
        "content_type": None,
        "title": "",
        "text": "",
        "links": [],
        "error": None,
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result["final_url"] = response.url
    result["http_status"] = response.status_code
    result["content_type"] = response.headers.get("Content-Type", "")

    if not response.ok:
        result["error"] = f"HTTP {response.status_code}"
        return result
    if "html" not in result["content_type"].lower():
        result["error"] = f"Not an HTML page ({result['content_type'] or 'no content type'})"
        return result

    soup = BeautifulSoup(response.content, "html.parser")
    if soup.title and soup.title.string:
        result["title"] = soup.title.string.strip()
    result["links"] = [a.get("href") for a in soup.find_all("a") if a.get("href")]
    if soup.body:
        for irrelevant in soup.body(["script", "style", "img", "input"]):
            irrelevant.decompose()
        result["text"] = soup.body.get_text(separator="\n", strip=True)
    return result
