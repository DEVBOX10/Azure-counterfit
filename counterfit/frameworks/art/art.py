
import numpy as np
import importlib
import inspect
import pathlib
import glob
import yaml
import re

from rich.table import Table
from counterfit.core.attacks import CFAttack
from counterfit.core.output import CFPrint
from counterfit.core.frameworks import CFFramework
from counterfit.core.targets import CFTarget

from .utils import attack_factory
from art.utils import compute_success_array, random_targets
from scipy.stats import entropy
from art.utils import clip_and_round

class ArtFramework(CFFramework):
    def __init__(self):
        super().__init__()

    @classmethod
    def get_attacks(cls, framework_path=f"{pathlib.Path(__file__).parent.resolve()}/attacks"):
        attacks = {}
        files = glob.glob(f"{framework_path}/*.yml")

        for attack in files:
            with open(attack, 'r') as f:
                data = yaml.safe_load(f)
        
            attacks[data['attack_name']] = data

        return attacks

    @classmethod
    def get_classifiers(cls):
        """
        Load ART classifiers
        """
        classifiers = {}
        base_import = importlib.import_module(f"art.estimators.classification")
        
        for classifier in base_import.__dict__.values():
            if inspect.isclass(classifier):
                if len(classifier.__subclasses__()) > 0:
                    for subclass in classifier.__subclasses__():
                        if "classification" not in subclass.__module__:
                            continue

                        if "classifier" in subclass.__module__:
                            continue

                        else:
                            classifier_name = re.findall(
                                r"\w+", str(subclass).split(".")[-1])[0]
                            classifiers[classifier_name] = subclass
        return classifiers

    @classmethod
    def build(cls, target: CFTarget, attack: str) -> object:
        """
        Build the attack.

        Initialize parameters.
        Set samples.
        """

        # Pass to helper factory function for attack creation.
        loaded_attack = attack_factory(attack)
        
        # Return the correct ART estimator for the target selected.
        # Keep an empty classifier around for extraction attacks.
        classifier = cls.set_classifier(target)

        # Build the blackbox classifier
        if "BlackBox" in classifier.__name__:
            target_classifier = classifier(
                target.predict_wrapper,
                target.input_shape,
                len(target.output_classes)
            )

        # Build the classifier if log_probs is present
        elif "QueryEfficientGradient" in classifier.__name__:
            class QEBBWrapper:
                def __init__(self) -> None:
                    class ModelWrapper:
                        predict = target.predict_wrapper
                    
                    self.model = ModelWrapper()
                    self.clip_values = loaded_attack.estimator.clip_values
                    self.nb_classes = loaded_attack.estimator.nb_classes
                    self.predict = target.predict_wrapper
            
            def class_gradient(x, label, target=target, num_basis=1, sigma=1.5, clip_values=(0., 255.), round_samples=0.0):
                epsilon_map = sigma * np.random.normal(size=([num_basis] + list(target.input_shape)))
                grads = []

                y = target.predict_wrapper(x[0])
                
                for i in range(len(x)):
                    minus = clip_and_round(
                        np.repeat(x[i], num_basis, axis=0) - epsilon_map,
                        clip_values,
                        round_samples,
                    )
                    plus = clip_and_round(
                        np.repeat(x[i], num_basis, axis=0) + epsilon_map,
                        clip_values,
                        round_samples,
                    )
                    
                    new_y_minus = np.array([entropy(y[i], p) for p in target.predict_wrapper(minus)])
                    new_y_plus = np.array([entropy(y[i], p) for p in target.predict_wrapper(plus)])
                    query_efficient_grad = 2 * np.mean(
                        np.multiply(
                            epsilon_map.reshape(num_basis, -1),
                            (new_y_plus - new_y_minus).reshape(num_basis, -1) / (2 * sigma),
                        ).reshape([-1] + list(target.input_shape)),
                        axis=0,
                    )
                    grads.append(query_efficient_grad)
#                 grads_array = self._apply_preprocessing_gradient(x, np.array(grads))

                return np.array(grads)                     

            temp_classifier = QEBBWrapper()
            setattr(temp_classifier, "class_gradient", class_gradient)

            target_classifier = classifier(
                classifier=temp_classifier,
                num_basis = 3,
                sigma = 1.5
            )

            setattr(target_classifier, "class_gradient", class_gradient)

        # Everything else takes a model file.
        else:
            target_classifier = classifier(
                model=target.model
            )

        loaded_attack._estimator = target_classifier

        return loaded_attack

    @classmethod
    def run(cls, cfattack: CFAttack):

        # Give the framework an opportunity to preprocess any thing in the attack.
        cls.pre_attack_processing(cfattack)
        
        # Find the appropriate "run" function
        attack_attributes = cfattack.attack.__dir__()

        # Run the attack. Each attack type has it's own execution function signature.
        if "infer" in attack_attributes:
            results = cfattack.attack.infer(cfattack.samples, np.array(cfattack.target.output_classes).astype(np.int64))
        elif "reconstruct" in attack_attributes:
            results = cfattack.attack.reconstruct(
                np.array(cfattack.samples, dtype=np.float32))

        elif "generate" in attack_attributes:
            original_inputs = np.array(cfattack.samples, dtype=np.float32)
            results = cfattack.attack.generate(x=original_inputs)

        elif "poison" in attack_attributes:
            results = cfattack.attack.poison(
                np.array(cfattack.samples, dtype=np.float32))

        elif "poison_estimator" in attack_attributes:
            results = cfattack.attack.poison(
                np.array(cfattack.samples, dtype=np.float32))

        elif "extract" in attack_attributes:
            # Returns a thieved classifier
            training_shape = (
                len(cfattack.target.X), *cfattack.target.input_shape)

            samples_to_query = cfattack.target.X.reshape(
                training_shape).astype(np.float32)
            results = cfattack.attack.extract(
                x=samples_to_query, thieved_classifier=cfattack.attack.estimator)

            cfattack.thieved_classifier = results
        else:
            print("Not found!")
        return results

    @classmethod
    def pre_attack_processing(cls, cfattack: CFAttack):
        cls.set_parameters(cfattack)

    @staticmethod
    def post_attack_processing(cfattack: CFAttack):
        pass

    @classmethod
    def set_classifier(cls, target: CFTarget):

        # Match the target.classifier attribute with an ART classifier type
        classifiers = cls.get_classifiers()

        # If no classifer attribute has been set, assume a closed-box.
        if not hasattr(target, "classifier"):

            # If the target model returns log_probs, return the relevant estimator
            if hasattr(target, "log_probs"):
                if target.log_probs == True:
                    return classifiers.get("QueryEfficientGradientEstimationClassifier")
            
            # Else return a plain BB estimator 
            else:
                return classifiers.get("BlackBoxClassifierNeuralNetwork")

        
        # Else resolve the correct classifier
        else:
            for classifier in classifiers.keys():
                # This translation is necessary as ART modules use the blackbox/whitebox naming convention for
                # closed-box/open-box attacks.
                target_classifier_name = target.classifier.lower()
                if target_classifier_name == 'closed-box':
                    target_classifier_name = 'blackbox'
                if target_classifier_name == 'open-box':
                    target_classifier_name = 'whitebox'
                if target_classifier_name in classifier.lower():
                    return classifiers.get(classifier, None)

    def check_success(self, cfattack: CFAttack) -> bool:
        attack_attributes = set(cfattack.attack.__dir__())

        if "generate" in attack_attributes:
            return self.evasion_success(cfattack)

        elif "extract" in attack_attributes:
            return self.extraction_success(cfattack)

    def evasion_success(self, cfattack: CFAttack):
        if cfattack.options.__dict__.get("targeted") == True:
            labels = cfattack.options.attack_parameters['target_labels']
            targeted = True
        else:
            labels = cfattack.initial_labels
            targeted = False

        success = compute_success_array(cfattack.attack._estimator, cfattack.samples, labels, cfattack.results, targeted)

        final_outputs, final_labels = cfattack.target.get_sample_labels(cfattack.results)
        cfattack.final_labels = final_labels
        cfattack.final_outputs = final_outputs
        return success

    def extraction_success(self, cfattack: CFAttack):
        training_shape = (
            len(cfattack.target.X), *cfattack.target.input_shape)
        training_data = cfattack.target.X.reshape(training_shape)

        victim_preds = np.atleast_1d(np.argmax(
            cfattack.target.predict_wrapper(x=training_data), axis=1))
        thieved_preds = np.atleast_1d(np.argmax(
            cfattack.thieved_classifier.predict(x=training_data), axis=1))

        acc = np.sum(victim_preds == thieved_preds) / len(victim_preds)

        cfattack.results = acc

        if acc > 0.1:  # TODO add to options struct
            return [True]
        else:
            return [False]

    @classmethod
    def set_parameters(cls, cfattack) -> None:
        # ART has its own set_params function. Use it.
        attack_params = {}
        for k, v in cfattack.options.attack_parameters.items():
            attack_params[k] = v["current"]
        cfattack.attack.set_params(**attack_params)
    
