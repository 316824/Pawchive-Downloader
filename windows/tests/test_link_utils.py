import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from link_utils import LinkInfo, extract_link, extract_links, extract_profile_url, parse_url


class LinkUtilsTests(unittest.TestCase):
    def test_profile_and_post_urls_are_canonicalized(self):
        info = parse_url('https://www.pawchive.pw/FanBox/user/artist-1/post/abc/?page=2#part')
        self.assertEqual(info, LinkInfo(
            'https://pawchive.pw/fanbox/user/artist-1',
            'https://pawchive.pw/fanbox/user/artist-1/post/abc'))

    def test_embedded_text_is_extracted_and_duplicates_are_removed(self):
        text = ('See https://pawchive.pw/fanbox/user/a/post/one, then '
                'https://pawchive.pw/fanbox/user/a/post/one. '
                'https://pawchive.pw/boosty/user/b')
        links = extract_links(text)
        self.assertEqual([item.post_url or item.profile_url for item in links], [
            'https://pawchive.pw/fanbox/user/a/post/one',
            'https://pawchive.pw/boosty/user/b'])

    def test_non_pawchive_or_invalid_urls_are_ignored(self):
        self.assertIsNone(extract_link('https://example.com/fanbox/user/a'))
        self.assertIsNone(extract_link('http://pawchive.pw/fanbox/user/a'))
        self.assertIsNone(parse_url('https://pawchive.pw/fanbox/user/a/post'))
        self.assertEqual(extract_profile_url('post: https://pawchive.pw/patreon/user/b/post/2'),
                         'https://pawchive.pw/patreon/user/b')


if __name__ == '__main__':
    unittest.main()
