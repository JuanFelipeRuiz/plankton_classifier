###########
# IMPORTS #
###########
import logging
import json
from lightning.pytorch.strategies import DDPStrategy
import pathlib
import sys
import json
from time import time

import lightning as l
import lightning.pytorch as pl
from lightning.pytorch.callbacks import LearningRateMonitor
import torch
from lightning.pytorch.loggers import CSVLogger, WandbLogger

from lit_ecology_classifier.data.datamodule import DataModule
from lit_ecology_classifier.helpers.argparser import pipeline_argparser
from lit_ecology_classifier.helpers.calc_class_weights import calculate_class_weights
from lit_ecology_classifier.helpers.helpers import setup_callbacks
from lit_ecology_classifier.models.model import LitClassifier
from lit_ecology_classifier.splitting.split_processor import SplitProcessor

# Start timing the script
time_begin = time()

# Configure logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - -%(message)s")

###############
# MAIN SCRIPT #
###############

if __name__ == "__main__":
    print("\nRunning", sys.argv[0], sys.argv[1:])

    # Parse Arguments for training
    parser = pipeline_argparser()
    args = parser.parse_args()

    # Create Output Directory if it doesn't exist
    pathlib.Path(args.train_outpath).mkdir(parents=True, exist_ok=True)

    logging.info(args)

    image_overview_path = pathlib.Path(args.dataset)/args.overview_filename

    split_overview_path = pathlib.Path(args.dataset)/f"split_overview.csv"

    pathlib.Path(args.dataset).mkdir(parents=True, exist_ok=True)

    split_processor = SplitProcessor(
                                split_overview_path = split_overview_path,
                                image_overview = image_overview_path,
                                split_hash = args.split_hash,
                                split_strategy = args.split_strategy,
                                filter_strategy =  args.filter_strategy,
                                split_args= args.split_args,
                                filter_args= args.filter_args,
                                class_map= args.class_map,
                                priority_classes= args.priority_classes,
                                rest_classes= args.rest_classes
                                )
    
    split_processor.save_split(description= args.description)
    split_overview = split_processor.get_split_df()

    # extract the class map from the split overview 
    args.class_map = {class_ if class_map != 0 else "rest" : class_map 
                       for class_, class_map in zip(split_overview["class"], split_overview["class_map"])}

    
    gpus =torch.cuda.device_count() if not args.no_gpu else 1
    logging.info(f"Using {gpus} GPUs for training.")
    
    gpus = 1

    datamodule = DataModule(**vars(args), splits=split_overview)
    datamodule.setup("fit")

    # TODO: not implemented in main, but could be useful. Find out if the implementation is still needed and correct

    #if args.balance_classes:
    #    class_weights=calculate_class_weights(datamodule.train_dataset)
    #    models.loss = torch.nn.CrossEntropyLoss(class_weights) if not "loss" in list(models.hparams) or not models.hparams.loss=="focal" else FocalLoss(alpha=class_weights ,gamma=1.75)
    # Initialize the loggers
    
    if args.use_wandb:
        logger = WandbLogger(
            project=args.dataset,
            log_model=False,
            save_dir=args.train_outpath,
        )
        logger.experiment.log_code("./lit_ecology_classifier", include_fn=lambda path: path.endswith(".py"))
    else:
        logger = CSVLogger(save_dir=args.train_outpath, name='csv_logs')

    torch.backends.cudnn.allow_tf32 = False

    args.num_classes = len(datamodule.class_map)
    if args.balance_classes:
        args.class_weights = calculate_class_weights(datamodule)
    else:
        args.class_weights = None

    
    model = LitClassifier(**vars(args), finetune=True)  # TODO: check if this works on cscs, maybe add a file that downlaods model first
    model.load_datamodule(datamodule)

    # Initialize the Trainer
    trainer = l.Trainer(
        logger=logger,
        max_epochs=args.max_epochs,
        log_every_n_steps=40,
        callbacks=[pl.callbacks.ModelCheckpoint(filename="best_model_acc_stage1", monitor="val_acc", mode="max"),LearningRateMonitor(logging_interval='step')],
        check_val_every_n_epoch=max(args.max_epochs // 8,1),
        devices=gpus,
        strategy= "ddp" if gpus > 0 else "auto" ,
        enable_progress_bar=False,
        default_root_dir=args.train_outpath,
    )

    # Train the first and last layer of the model
    trainer.fit(model, datamodule=datamodule)

    # Load the best model from the first stage
    model = LitClassifier.load_from_checkpoint(str(trainer.checkpoint_callback.best_model_path), lr=args.lr * args.lr_factor, pretrained=False)
    model.load_datamodule(datamodule)
    
    # sets up callbacks for stage 2
    callbacks = setup_callbacks(args.priority_classes, "best_model_acc_stage2")

    trainer = pl.Trainer(
        logger=logger,
        max_epochs=2 * args.max_epochs,
        log_every_n_steps=40,
        callbacks=callbacks,
        check_val_every_n_epoch=max(args.max_epochs // 8,1),
        devices=gpus,
        strategy="ddp" if gpus > 0 else "auto",
        enable_progress_bar=False,
        default_root_dir=args.train_outpath,
    )
    trainer.fit(model, datamodule=datamodule)

    # Calculate and log the total time taken for training
    total_secs = -1 if time_begin is None else (time() - time_begin)
    logging.info("Time taken for training (in secs): {}".format(total_secs))
