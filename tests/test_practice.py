import math
import random
import struct
import unittest

from app.practice import answer_matches, marked_key, new_practice_card


def circle_stroke(key, *, points=96, radius_x=None, radius_y=None):
    radius_x = radius_x or key.width / 2 + 28
    radius_y = radius_y or key.height / 2 + 22
    center_x, center_y = key.center
    return [
        [center_x + radius_x * math.cos(math.tau * index / points),
         center_y + radius_y * math.sin(math.tau * index / points)]
        for index in range(points + 1)
    ]


class PracticeCardTests(unittest.TestCase):
    def test_renders_three_distinct_random_alphanumeric_keycaps(self):
        card = new_practice_card(random.Random(31))

        self.assertEqual((card.width, card.height), (1200, 720))
        self.assertEqual(len(card.keycaps), 3)
        self.assertEqual(len(set(card.labels)), 3)
        self.assertTrue(all(2 <= len(label) <= 3 for label in card.labels))
        self.assertTrue(all(any(char.isdigit() for char in label) for label in card.labels))
        self.assertTrue(all(any(char.isalpha() for char in label) for label in card.labels))
        self.assertTrue(card.png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", card.png[16:24]), (card.width, card.height))
        self.assertEqual(card.note, "Read the key I circled. Reply with only its label.")

    def test_mark_geometry_selects_the_one_circled_key(self):
        card = new_practice_card(random.Random(11))
        chosen = card.keycaps[1]

        self.assertEqual(marked_key({"strokes": [circle_stroke(chosen)]}, card), chosen.label)

    def test_line_or_two_circles_cannot_verify(self):
        card = new_practice_card(random.Random(17))
        key = card.keycaps[0]
        center_x, center_y = key.center
        line = [[center_x - 100 + 200 * i / 30, center_y] for i in range(31)]
        self.assertIsNone(marked_key({"strokes": [line]}, card))
        self.assertIsNone(
            marked_key({"strokes": [circle_stroke(card.keycaps[0]), circle_stroke(card.keycaps[2])]}, card)
        )

    def test_answer_allows_punctuation_and_explanation_but_not_wrong_or_multiple_keys(self):
        labels = ("K7", "M4Q", "8R")
        self.assertTrue(answer_matches("(M4Q). That's the circled key.", "M4Q", labels))
        self.assertTrue(answer_matches("The answer: M4Q!", "M4Q", labels))
        self.assertFalse(answer_matches("K7", "M4Q", labels))
        self.assertFalse(answer_matches("M4Q or K7", "M4Q", labels))
        self.assertFalse(answer_matches("I can't tell.", "M4Q", labels))
        self.assertFalse(answer_matches("M4Q", "M4Q", ("K7", "8R")))


if __name__ == "__main__":
    unittest.main()
